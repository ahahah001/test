# Copyright (c) Microsoft Corporation.
# Licensed under the MIT license.
ANNOTATION_DIR=$1
SOURCE_SPLIT=$2
OUT_DIR=$3

WORK_DIR=$(readlink -f .)
DATA_DIR=${WORK_DIR}/data
PRETRAIN_DIR=${DATA_DIR}/pretrained

echo "extracting text features..."
if [ ! -d $OUT_DIR ]; then
  mkdir -p $OUT_DIR
fi

docker run --ipc=host --rm \
  --mount src="${WORK_DIR}",dst=/src,type=bind \
  --mount src="$PRETRAIN_DIR",dst=/pretrain,type=bind,readonly \
  --mount src=$ANNOTATION_DIR,dst=/annotation,type=bind,readonly \
  --mount src=$OUT_DIR,dst=/output,type=bind \
  -w /src vimos/uniter_ve:latest \
  bash -c "python preprocess.py --annotation $SOURCE_SPLIT"

echo "done"
