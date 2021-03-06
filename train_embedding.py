"""
Copyright (c) VisualJoyce.
Licensed under the MIT license.
"""
import argparse
import glob
import os
import re
import shutil
from collections import Counter
from itertools import chain
from os.path import exists, join
from time import time

import numpy as np
import torch
from horovod import torch as hvd
# from apex import amp
from sklearn.metrics.pairwise import cosine_similarity
from torch.cuda.amp import autocast, GradScaler
from torch.nn import functional as F
from torch.nn.utils import clip_grad_norm_
from tqdm import tqdm

from chengyubert.data import create_dataloaders
from chengyubert.data.dataset import DATA_REGISTRY
from chengyubert.data.evaluation import judge
from chengyubert.models import build_model
from chengyubert.optim import get_lr_sched
from chengyubert.optim.misc import build_optimizer
from chengyubert.utils.distributed import (all_reduce_and_rescale_tensors, all_gather_list,
                                           broadcast_tensors)
from chengyubert.utils.logger import LOGGER, TB_LOGGER, RunningMeter, add_log_to_file
from chengyubert.utils.misc import NoOp, parse_with_config, set_dropout, set_random_seed
from chengyubert.utils.save import ModelSaver, save_training_meta


def train(model, dataloaders, opts):
    # make sure every process has same model parameters in the beginning
    broadcast_tensors([p.data for p in model.parameters()], 0)
    set_dropout(model, opts.dropout)

    # Prepare optimizer
    optimizer = build_optimizer(model, opts)
    scaler = GradScaler()

    global_step = 0
    if opts.rank == 0:
        save_training_meta(opts)
        TB_LOGGER.create(join(opts.output_dir, 'log'))
        pbar = tqdm(total=opts.num_train_steps, desc=opts.model)
        model_saver = ModelSaver(join(opts.output_dir, 'ckpt'))
        os.makedirs(join(opts.output_dir, 'results'), exist_ok=True)  # store val predictions
        add_log_to_file(join(opts.output_dir, 'log', 'log.txt'))
    else:
        LOGGER.disabled = True
        pbar = NoOp()
        model_saver = NoOp()

    LOGGER.info(f"***** Running training with {opts.n_gpu} GPUs *****")
    LOGGER.info("  Num examples = %d", len(dataloaders['train'].dataset))
    LOGGER.info("  Batch size = %d", opts.train_batch_size)
    LOGGER.info("  Accumulate steps = %d", opts.gradient_accumulation_steps)
    LOGGER.info("  Num steps = %d", opts.num_train_steps)

    running_loss = RunningMeter('loss')
    model.train()
    n_examples = 0
    n_epoch = 0
    best_ckpt = 0
    best_eval = 0
    start = time()
    # quick hack for amp delay_unscale bug
    optimizer.zero_grad()
    optimizer.step()
    while True:
        for step, batch in enumerate(dataloaders['train']):
            targets = batch['targets']
            del batch['gather_index']
            n_examples += targets.size(0)

            with autocast():
                loss = model(**batch, compute_loss=True)
                loss = loss.mean()

            delay_unscale = (step + 1) % opts.gradient_accumulation_steps != 0
            scaler.scale(loss).backward()
            if not delay_unscale:
                # gather gradients from every processes
                # do this before unscaling to make sure every process uses
                # the same gradient scale
                grads = [p.grad.data for p in model.parameters()
                         if p.requires_grad and p.grad is not None]
                all_reduce_and_rescale_tensors(grads, float(1))

            running_loss(loss.item())

            if (step + 1) % opts.gradient_accumulation_steps == 0:
                global_step += 1

                # learning rate scheduling
                lr_this_step = get_lr_sched(global_step, opts)
                for param_group in optimizer.param_groups:
                    param_group['lr'] = lr_this_step
                TB_LOGGER.add_scalar('lr', lr_this_step, global_step)

                # log loss
                losses = all_gather_list(running_loss)
                running_loss = RunningMeter(
                    'loss', sum(l.val for l in losses) / len(losses))
                TB_LOGGER.add_scalar('loss', running_loss.val, global_step)
                TB_LOGGER.step()

                # update model params
                if opts.grad_norm != -1:
                    # Unscales the gradients of optimizer's assigned params in-place
                    scaler.unscale_(optimizer)
                    grad_norm = clip_grad_norm_(model.parameters(), opts.grad_norm)
                    TB_LOGGER.add_scalar('grad_norm', grad_norm, global_step)

                # scaler.step() first unscales gradients of the optimizer's params.
                # If gradients don't contain infs/NaNs, optimizer.step() is then called,
                # otherwise, optimizer.step() is skipped.
                scaler.step(optimizer)

                # Updates the scale for next iteration.
                scaler.update()
                optimizer.zero_grad()
                pbar.update(1)

                if global_step % 100 == 0:
                    # monitor training throughput
                    tot_ex = sum(all_gather_list(n_examples))
                    ex_per_sec = int(tot_ex / (time() - start))
                    LOGGER.info(f'{opts.model}: {n_epoch}-{global_step}: '
                                f'{tot_ex} examples trained at '
                                f'{ex_per_sec} ex/s '
                                f'best_acc-{best_eval * 100:.2f}')
                    TB_LOGGER.add_scalar('perf/ex_per_s',
                                         ex_per_sec, global_step)

                if global_step % opts.valid_steps == 0:
                    log = evaluation(model,
                                     dict(filter(lambda x: x[0].startswith('val'), dataloaders.items())),
                                     opts, global_step)
                    log_eval = log['val/acc']
                    if log_eval > best_eval:
                        best_ckpt = global_step
                        best_eval = log_eval
                        pbar.set_description(f'{opts.model}: {n_epoch}-{best_ckpt} best_acc-{best_eval * 100:.2f}')
                    model_saver.save(model, global_step)
            if global_step >= opts.num_train_steps:
                break
        if global_step >= opts.num_train_steps:
            break
        n_epoch += 1
        LOGGER.info(f"Step {global_step}: finished {n_epoch} epochs")
        # if n_epoch >= opts.num_train_epochs:
        #     break
    return best_ckpt


@torch.no_grad()
def validate(opts, model, val_loader, split, global_step):
    val_loss = 0
    tot_score = 0
    n_ex = 0
    val_mrr = 0
    st = time()
    results = []
    with tqdm(range(len(val_loader.dataset) // opts.size), desc=f'{split}-{opts.rank}') as tq:
        for i, batch in enumerate(val_loader):
            qids = batch['qids']
            targets = batch['targets']
            del batch['targets']
            del batch['qids']
            del batch['gather_index']

            scores, over_logits = model(**batch, targets=None, compute_loss=False)
            loss = F.cross_entropy(scores, targets, reduction='sum')
            val_loss += loss.item()
            tot_score += (scores.max(dim=-1, keepdim=False)[1] == targets).sum().item()
            max_prob, max_idx = scores.max(dim=-1, keepdim=False)
            answers = max_idx.cpu().tolist()

            targets = torch.gather(batch['option_ids'], dim=1, index=targets.unsqueeze(1)).cpu().numpy()
            for j, (qid, target) in enumerate(zip(qids, targets)):
                g = over_logits[j].cpu().numpy()
                top_k = np.argsort(-g)
                val_mrr += 1 / (1 + np.argwhere(top_k == target).item())

            results.extend(zip(qids, answers))
            n_ex += len(qids)
            tq.update(len(qids))

    out_file = f'{opts.output_dir}/results/{split}_results_{global_step}_rank{opts.rank}.csv'
    with open(out_file, 'w') as f:
        for id_, ans in results:
            f.write(f'{id_},{ans}\n')

    val_loss = sum(all_gather_list(val_loss))
    val_mrr = sum(all_gather_list(val_mrr))
    # tot_score = sum(all_gather_list(tot_score))
    n_ex = sum(all_gather_list(n_ex))
    tot_time = time() - st

    val_loss /= n_ex
    val_mrr = val_mrr / n_ex

    out_file = f'{opts.output_dir}/results/{split}_results_{global_step}.csv'
    if not os.path.isfile(out_file):
        with open(out_file, 'wb') as g:
            for f in glob.glob(f'{opts.output_dir}/results/{split}_results_{global_step}_rank*.csv'):
                shutil.copyfileobj(open(f, 'rb'), g)

    sum(all_gather_list(opts.rank))

    txt_db = getattr(opts, f'{split}_txt_db')
    val_acc = judge(out_file, f'{txt_db}/answer.csv')
    val_log = {f'{split}/loss': val_loss,
               f'{split}/acc': val_acc,
               f'{split}/mrr': val_mrr,
               f'{split}/ex_per_s': n_ex / tot_time}
    LOGGER.info(f"validation finished in {int(tot_time)} seconds, "
                f"score: {val_acc * 100:.2f}, "
                f"mrr: {val_mrr:.3f}")
    return val_log


def evaluate_embeddings_recall(embeddings, chengyu_vocab, chengyu_synonyms_dict):
    iw = [k for k in embeddings]

    vectors = np.array([embeddings[iw[i]] for i in range(len(iw))])
    cnt = 0
    recall_at_k_cosine = {}
    recall_at_k_norm = {}
    k_list = [1, 3, 5, 10]
    total = len(chengyu_synonyms_dict)
    for w, wl in tqdm(chengyu_synonyms_dict.items()):
        if w in embeddings and any([x in embeddings for x in wl]):
            cnt += 1

            cosine_distances = (1 - cosine_similarity(embeddings[w].reshape(1, -1), vectors)[0]).argsort()
            norm_distances = np.linalg.norm(vectors - embeddings[w], axis=1).argsort()
            cids = [idx for idx in cosine_distances if iw[idx] in chengyu_vocab]
            nids = [idx for idx in norm_distances if iw[idx] in chengyu_vocab]
            for k in k_list:
                top_ids = cids[1:k + 1]
                recall_at_k_cosine.setdefault(k, 0)
                recall_at_k_cosine[k] += sum([1 for idx in top_ids if iw[idx] in wl])

                top_ids = nids[1:k + 1]
                recall_at_k_norm.setdefault(k, 0)
                recall_at_k_norm[k] += sum([1 for idx in top_ids if iw[idx] in wl])
    LOGGER.info(f'{cnt} word pairs appeared in the training dictionary , total word pairs {total}')
    LOGGER.info(recall_at_k_cosine)
    LOGGER.info(recall_at_k_norm)
    return cnt, total, recall_at_k_cosine, recall_at_k_norm


def evaluation(model, data_loaders: dict, opts, global_step):
    model.eval()
    log = {}
    for split, loader in data_loaders.items():
        LOGGER.info(f"Step {global_step}: start running "
                    f"validation on {split} split...")
        log.update(validate(opts, model, loader, split, global_step))
        if split == 'val' and opts.evaluate_embedding:
            embeddings_np = model.idiom_embedding.weight.detach().cpu().numpy()
            embeddings = {k: embeddings_np[v] for k, v in loader.dataset.chengyu_vocab.items()}
            evaluate_embeddings_recall(embeddings, loader.dataset.chengyu_vocab,
                                       loader.dataset.chengyu_synonyms_dict)
    TB_LOGGER.log_scaler_dict(log)
    model.train()
    return log


def get_best_ckpt(val_data_dir, opts):
    pat = re.compile(r'val_results_(?P<step>\d+)_rank0.csv')
    prediction_files = glob.glob('{}/results/val_results_*_rank0.csv'.format(opts.output_dir))

    top_files = Counter()
    for f in prediction_files:
        acc = judge(f, os.path.join(val_data_dir, 'answer.csv'))
        top_files.update({f: acc})

    print(top_files)

    for f, acc in top_files.most_common(1):
        m = pat.match(os.path.basename(f))
        best_epoch = int(m.group('step'))
        return best_epoch


def main(opts):
    device = torch.device("cuda", hvd.local_rank())
    torch.cuda.set_device(hvd.local_rank())
    rank = hvd.rank()
    opts.rank = rank
    opts.size = hvd.size()
    LOGGER.info("device: {} n_gpu: {}, rank: {}, "
                "16-bits training: {}".format(
        device, n_gpu, hvd.rank(), opts.fp16))

    if opts.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, "
                         "should be >= 1".format(
            opts.gradient_accumulation_steps))

    set_random_seed(opts.seed)

    # data loaders
    DatasetCls = DATA_REGISTRY[opts.dataset_cls]
    EvalDatasetCls = DATA_REGISTRY[opts.eval_dataset_cls]
    splits, dataloaders = create_dataloaders(DatasetCls, EvalDatasetCls, opts)
    opts.evaluate_embedding = True

    # Prepare model
    model = build_model(opts)
    model.to(device)

    if opts.mode == 'train':
        best_ckpt = train(model, dataloaders, opts)
    elif opts.mode == 'eval':
        best_ckpt = None
        if opts.rank == 0:
            os.makedirs(join(opts.output_dir, 'results'), exist_ok=True)  # store val predictions
    else:
        best_ckpt = get_best_ckpt(dataloaders['val'].dataset.db_dir, opts)

    sum(all_gather_list(opts.rank))

    if best_ckpt is not None:
        best_pt = f'{opts.output_dir}/ckpt/model_step_{best_ckpt}.pt'
        model.load_state_dict(torch.load(best_pt), strict=False)
    log = evaluation(model, dict(filter(lambda x: x[0] != 'train', dataloaders.items())), opts, best_ckpt)
    splits = ['val', 'test', 'ran', 'sim', 'out']
    LOGGER.info('\t'.join(splits))
    LOGGER.info('\t'.join(chain(
        [format(log[f'{split}/acc'], "0.6f") for split in splits],
        [format(log[f'{split}/mrr'], "0.6f") for split in splits]
    )))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()

    # Required parameters
    parser.add_argument('--compressed_db', action='store_true',
                        help='use compressed LMDB')
    parser.add_argument("--model_config",
                        default=None, type=str,
                        help="json file for model architecture")
    parser.add_argument("--checkpoint",
                        default=None, type=str,
                        help="pretrained model")
    parser.add_argument("--model", default='paired',
                        choices=['snlive'],
                        help="choose from 2 model architecture")
    parser.add_argument("--mode", default='train',
                        choices=['train', 'infer', 'eval'],
                        help="choose from 2 mode")

    parser.add_argument("--output_dir", default=None, type=str,
                        help="The output directory where the model checkpoints will be "
                             "written.")

    # Prepro parameters
    parser.add_argument('--max_txt_len', type=int, default=60,
                        help='max number of tokens in text (BERT BPE)')
    parser.add_argument('--conf_th', type=float, default=0.2,
                        help='threshold for dynamic bounding boxes '
                             '(-1 for fixed)')
    parser.add_argument('--max_bb', type=int, default=100,
                        help='max number of bounding boxes')
    parser.add_argument('--min_bb', type=int, default=10,
                        help='min number of bounding boxes')
    parser.add_argument('--num_bb', type=int, default=36,
                        help='static number of bounding boxes')

    # training parameters
    parser.add_argument("--train_batch_size",
                        default=4096, type=int,
                        help="Total batch size for training. "
                             "(batch by tokens)")
    parser.add_argument("--val_batch_size",
                        default=4096, type=int,
                        help="Total batch size for validation. "
                             "(batch by tokens)")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=16,
                        help="Number of updates steps to accumualte before "
                             "performing a backward/update pass.")
    parser.add_argument("--learning_rate",
                        default=3e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--valid_steps",
                        default=1000,
                        type=int,
                        help="Run validation every X steps")
    parser.add_argument("--num_train_steps",
                        default=100000,
                        type=int,
                        help="Total number of training updates to perform.")
    parser.add_argument("--optim", default='adam',
                        choices=['adam', 'adamax', 'adamw'],
                        help="optimizer")
    parser.add_argument("--betas", default=[0.9, 0.98], nargs='+', type=float,
                        help="beta for adam optimizer")
    parser.add_argument("--dropout",
                        default=0.1,
                        type=float,
                        help="tune dropout regularization")
    parser.add_argument("--weight_decay",
                        default=0.0,
                        type=float,
                        help="weight decay (L2) regularization")
    parser.add_argument("--grad_norm",
                        default=0.25,
                        type=float,
                        help="gradient clipping (-1 for no clipping)")
    parser.add_argument("--warmup_steps",
                        default=4000,
                        type=int,
                        help="Number of training steps to perform linear "
                             "learning rate warmup for.")

    # device parameters
    parser.add_argument('--seed',
                        type=int,
                        default=42,
                        help="random seed for initialization")
    parser.add_argument('--fp16',
                        action='store_true',
                        help="Whether to use 16-bit float precision instead "
                             "of 32-bit")
    parser.add_argument('--n_workers', type=int, default=4,
                        help="number of data workers")
    parser.add_argument('--pin_mem', action='store_true',
                        help="pin memory")

    # can use config files
    parser.add_argument('--config', help='JSON config files')

    args = parse_with_config(parser)

    hvd.init()
    n_gpu = hvd.size()
    args.n_gpu = n_gpu

    args.output_dir = os.path.join(args.output_dir,
                                   args.model,
                                   os.path.basename(args.pretrained_model_name_or_path),
                                   f'pretrain_{args.n_gpu}_{args.num_train_steps}_{args.learning_rate}_{args.max_txt_len}')

    if exists(args.output_dir) and os.listdir(f'{args.output_dir}/ckpt'):
        if args.mode == 'train':
            raise ValueError("Output directory ({}) already exists and is not "
                             "empty.".format(args.output_dir))

    if args.conf_th == -1:
        assert args.max_bb + args.max_txt_len + 2 <= 512
    else:
        assert args.num_bb + args.max_txt_len + 2 <= 512

    main(args)
