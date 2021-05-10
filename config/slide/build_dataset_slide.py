import json
import os
import random
import sys

import jsonlines
import pandas as pda
import spacy
from sklearn.model_selection import train_test_split
from tqdm import tqdm

random_state = 42
random.seed(random_state)

annotation_dir = sys.argv[1]

nlp = spacy.load('en_core_web_sm')

df_sentiment = pda.read_csv(f"{annotation_dir}/idiomLexicon.tsv", sep="\t")
df_sentiment = df_sentiment[df_sentiment['Maj. Label'] != 'inappropriate']

sentiment_map = {}
for item in df_sentiment.itertuples():
    sentiment_map[item.Idiom] = item._11

possessive_form1 = {
    "one",
    "someone",
    "anyone"
}

possessive_form2 = {'my', 'your', 'his', 'her', 'our', 'their'}


def parse_start_end(sentence, span):
    doc = nlp(sentence)
    target_doc = nlp(span)

    start, end = None, None
    for i, t in enumerate(doc):
        j = i
        trues = []
        for tt in target_doc:
            if t.is_punct:
                j += 1
            elif tt.is_punct or tt.text == "'s":
                continue
            else:
                checks = [
                    t.text.lower() == tt.text.lower(),
                    t.text.lower() in possessive_form2 and tt.text.lower() in possessive_form1,
                    t.lemma_.lower() == tt.lemma_.lower()
                ]
                #                 print(t.text.lower(), tt.text.lower(), checks)
                if any(checks):
                    trues.append(True)
                else:
                    trues.append(False)
                    break
                j += 1
            t = doc[j]
        #         print(trues)
        if trues and all(trues):
            start = i
            end = j - 1
            break

    if start is None or end is None:
        print(sentence, span)

    assert start is not None and end is not None
    return doc, start, end


def is_substring(s_text, idiom):
    return s_text.lower().replace('-', ' ').replace(' ', '').count(
        idiom.lower().replace('-', ' ').replace(' ', '')) == 1


if __name__ == '__main__':
    from more_itertools import chunked

    idioms_set = set(df_sentiment.Idiom.tolist())

    idiom_definitions = {}
    for _, idiom, explanation in chunked(open(f'{annotation_dir}/idioms_dataset_2432').read().split('\n'), 3):
        idiom_definitions[idiom] = explanation

    idioms_extra_set = set(idiom_definitions.keys())

    # We want to use 580 for evaluation, so avoid adding these idioms to training and validation
    df_idioms_580 = pda.read_csv(f'{annotation_dir}/idioms_580.csv')
    idioment_set = set(df_idioms_580.idiom.tolist())

    intersections = set(idioms_set).intersection(idioment_set)

    unlabelled = idioms_extra_set.difference(idioms_set.union(idioment_set))

    total = df_sentiment.shape[0]
    df_sentiment_no_intersection = df_sentiment[~df_sentiment.Idiom.isin(intersections)]
    X_train, X_test, y_train, y_test = train_test_split(df_sentiment_no_intersection.Idiom,
                                                        df_sentiment_no_intersection['Maj. Label'],
                                                        test_size=int(0.2 * total - len(intersections)),
                                                        stratify=df_sentiment_no_intersection['Maj. Label'],
                                                        random_state=random_state)
    X_train, X_dev, y_train, y_dev = train_test_split(X_train,
                                                      y_train,
                                                      test_size=0.25,
                                                      stratify=y_train,
                                                      random_state=random_state)

    train = [k for k in X_train.tolist()]
    dev = [k for k in X_dev.tolist()]
    test = [k for k in X_test.tolist()] + list(intersections)

    idiom_span_mapping = {}
    data = {}
    all_idioms = idioms_set.union(idioment_set).union(idioms_extra_set)
    for idiom in tqdm(all_idioms):
        dump_files = [
            f'{annotation_dir}/bnc_dumped/{idiom}.jsonl',
            f'{annotation_dir}/1billion_dumped/{idiom}.jsonl',
        ]
        for dump_file in dump_files:
            if os.path.isfile(dump_file) and os.stat(dump_file).st_size > 0:
                with jsonlines.open(dump_file) as f:
                    for d in f:
                        if len(d['content']) > 1:
                            content = ''
                            for k in sorted(d['content']):
                                v = d['content'][k]
                                if k == d['_id']:
                                    v = v.replace('<em>', '').replace('</em>', '')
                                content += v
                        else:
                            content = d['content'][d['_id']].replace('<em>', '').replace('</em>', '')

                        span_text = d['groundTruth'][0]
                        if content.count(span_text) == 1 and len(span_text) - len(idiom) < len(idiom):
                            d['idiom'] = idiom
                            d['content'] = content.replace(span_text, "#idiom#")
                            if ' #idiom# ' in d['content']:
                                if span_text in idiom_span_mapping and idiom_span_mapping[span_text] != d['idiom']:
                                    print("Error:", d, span_text, idiom_span_mapping[span_text])
                                else:
                                    data.setdefault(idiom, [])
                                    data[idiom].append(d)
                                    idiom_span_mapping[span_text] = d['idiom']

    with open(f'{annotation_dir}/data.json', mode='w') as fp:
        json.dump(data, fp, ensure_ascii=False, indent=2)

    with open(f'{annotation_dir}/train.json', mode='w') as f:
        json.dump(train, f, ensure_ascii=False, indent=2)
    with open(f'{annotation_dir}/dev.json', mode='w') as f:
        json.dump(dev, f, ensure_ascii=False, indent=2)
    with open(f'{annotation_dir}/test.json', mode='w') as f:
        json.dump(test, f, ensure_ascii=False, indent=2)
    with open(f'{annotation_dir}/unlabelled.json', mode='w') as f:
        json.dump(list(unlabelled), f, ensure_ascii=False, indent=2)
    with open(f'{annotation_dir}/idiom_span_mapping.json', mode='w') as f:
        json.dump(idiom_span_mapping, f, ensure_ascii=False, indent=2)
