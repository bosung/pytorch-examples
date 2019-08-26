This is testbed for deep learning experiments based on [OpenNMT](https://github.com/OpenNMT/OpenNMT-py) code

### preprocess
```angular2html
python preprocess.py --train_src=data/semeval/train.tsv --valid_src=data/semeval/dev.tsv --save_data=data/semeval/temp
```

### train
```angular2html
python train.py -data data/semeval/temp -save_model temp-model -gpu_ranks 0
```
