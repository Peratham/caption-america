#!/bin/bash

echo "Downloading the COCO dataset"
mkdir $HOME/data
./download_coco_dataset.sh $HOME/data
python load_grefexp_to_redis.py $HOME/data
python train.py model.h5
python caption.py model.h5 cat.jpg
