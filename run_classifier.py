# coding=utf-8
# Copyright 2018 The Google AI Language Team Authors and The HuggingFace Inc. team.
# Copyright (c) 2018, NVIDIA CORPORATION.  All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""BERT finetuning runner."""

from __future__ import absolute_import, division, print_function

import argparse
import glob
import json
import logging
import os
import random
import sys

import numpy as np
import math
import torch
import torch.nn as nn
from torch.utils.data import (DataLoader, RandomSampler, SequentialSampler, TensorDataset)
from torch.utils.data.distributed import DistributedSampler
from tqdm import tqdm, trange

from torch.nn import CrossEntropyLoss, MSELoss, Sigmoid, KLDivLoss, Softmax, LogSoftmax, BCEWithLogitsLoss, TripletMarginLoss, SoftMarginLoss
from scipy.stats import pearsonr, spearmanr
from sklearn.metrics import matthews_corrcoef, f1_score, precision_recall_fscore_support

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))
from onmt.encoders.bi_lstm_encoder import RNNEncoder
from models.vgg import VGG
from data_processor import *
from wikiqa_eval import wikiqa_eval
from semeval_eval import semeval_eval
from ranking_eval import ranking_eval
from ploting.output_ploting import plot_samples, plot_out_dist, plot_vector_bar

from setproctitle import setproctitle
# from tensorboardX import SummaryWriter
from torch.utils.tensorboard import SummaryWriter

from collections import Counter
import spacy

from transformers import (
    WEIGHTS_NAME,
    AdamW,
    BertConfig,
    BertForSequenceClassification,
    BertTokenizer,
    get_linear_schedule_with_warmup,
)

spacy_en = spacy.load('en_core_web_sm')


class simple_tokenizer():
    def tokenize(self, text):
        return [tok.text for tok in spacy_en.tokenizer(text)]


setproctitle("(bosung) bert classifier")
logger = logging.getLogger(__name__)

nnSoftmax = Softmax(dim=0)
nnLogSoftmax = LogSoftmax(dim=0)


class InputFeatures(object):
    """A single set of features of data."""

    def __init__(self, input_ids, input_mask, segment_ids, label_id, weight=0.01, preprob=0.0):
        self.input_ids = input_ids
        self.input_mask = input_mask
        self.segment_ids = segment_ids
        self.label_id = label_id
        self.weight = weight
        self.preprob = preprob


class SimpleFeatures(object):
    """A single set of features of data."""

    def __init__(self, vector, label_id, weight=0.01, preprob=0.0):
        self.vector = vector
        self.label_id = label_id
        self.weight = weight
        self.preprob = preprob


class InputPairFeatures(object):
    """A single set of features of data."""

    def __init__(self, input_ids_a, input_ids_b, label_id, weight=0.01, preprob=0.0):
        self.input_ids_a = input_ids_a
        self.input_ids_b = input_ids_b
        self.label_id = label_id
        self.weight = weight
        self.preprob = preprob


def divide_features_by_label(examples, labels):
    features = []
    features_by_label = [[], []]
    for vector, label in zip(examples, labels):
        features.append(SimpleFeatures(vector, label))
        if label == 0:  # cat
            features_by_label[0].append(SimpleFeatures(vector, 0))
        else:
            assert label == 1  # dog
            features_by_label[1].append(SimpleFeatures(vector, 1))

    print("[CIFAR-10 (Binary) ] (train) label 0: %d  / label 1: %d" % (
        len(features_by_label[0]), len(features_by_label[1])))
    return features, features_by_label


def convert_examples_to_features_rnn(word2idx_dict, examples, label_list, _max_seq_length,
                                     tokenizer, output_mode, sep=False):
    """Loads a data file into a list of `InputBatch`s."""

    def _word2idx(tokens):
        ids = []
        for t in tokens:
            if t in word2idx_dict:
                ids.append(word2idx_dict[t])
            else:
                ids.append(1)
        return ids

    max_seq_length = 64

    label_map = {label: i for i, label in enumerate(label_list)}

    features = []
    features_by_label = [[] for _ in range(len(label_list))]  # [[label 0 data], [label 1 data] ... []]

    for ex_index, example in enumerate(examples):

        tokens_a = tokenizer.tokenize(example.text_a)
        tokens_b = tokenizer.tokenize(example.text_b)

        input_ids_a = _word2idx(tokens_a)
        input_ids_b = _word2idx(tokens_b)

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.

        # Zero-pad up to the sequence length.
        if len(input_ids_a) < max_seq_length:
            padding_a = [0] * (max_seq_length - len(input_ids_a))
            input_ids_a += padding_a
        else:
            input_ids_a = input_ids_a[:max_seq_length]

        if len(input_ids_b) < max_seq_length:
            padding_b = [0] * (max_seq_length - len(input_ids_b))
            input_ids_b += padding_b
        else:
            input_ids_b = input_ids_b[:max_seq_length]

        assert len(input_ids_a) == max_seq_length
        assert len(input_ids_b) == max_seq_length

        if output_mode == "classification":
            label_id = label_map[example.label]
        elif output_mode == "regression":
            label_id = float(example.label)
        else:
            raise KeyError(output_mode)

        if ex_index < 5:
            logger.info("*** Example ***")
            logger.info("guid: %s" % (example.guid))
            logger.info("tokens A: %s" % " ".join([str(x) for x in tokens_a]))
            logger.info("tokens B: %s" % " ".join([str(x) for x in tokens_b]))
            logger.info("input_ids A: %s" % " ".join([str(x) for x in input_ids_a]))
            logger.info("input_ids B: %s" % " ".join([str(x) for x in input_ids_b]))
            logger.info("label: %s (id = %d)" % (example.label, label_id))

        features.append(
            InputPairFeatures(input_ids_a=input_ids_a, input_ids_b=input_ids_b, label_id=label_id))

        features_by_label[label_id].append(
            InputPairFeatures(input_ids_a=input_ids_a, input_ids_b=input_ids_b, label_id=label_id))

    if sep is False:
        return features
    else:
        assert len(features) == (len(features_by_label[0]) + len(features_by_label[1]))
        logger.info(" total:  %d\tlabel 0: %d\tlabel 1: %d " % (
            len(features), len(features_by_label[0]), len(features_by_label[1])))
        return features, features_by_label


def convert_examples_to_features(examples, label_list, max_seq_length,
                                 tokenizer, output_mode, sep=False):
    """Loads a data file into a list of `InputBatch`s."""

    label_map = {label: i for i, label in enumerate(label_list)}

    features = []
    features_by_label = [[] for _ in range(len(label_list))]  # [[label 0 data], [label 1 data] ... []]

    for (ex_index, example) in enumerate(examples):
        if ex_index % 10000 == 0:
            logger.info("Writing example %d of %d" % (ex_index, len(examples)))

        tokens_a = tokenizer.tokenize(example.text_a)

        tokens_b = None
        if example.text_b:
            tokens_b = tokenizer.tokenize(example.text_b)
            # Modifies `tokens_a` and `tokens_b` in place so that the total
            # length is less than the specified length.
            # Account for [CLS], [SEP], [SEP] with "- 3"
            _truncate_seq_pair(tokens_a, tokens_b, max_seq_length - 3)
        else:
            # Account for [CLS] and [SEP] with "- 2"
            if len(tokens_a) > max_seq_length - 2:
                tokens_a = tokens_a[:(max_seq_length - 2)]

        # The convention in BERT is:
        # (a) For sequence pairs:
        #  tokens:   [CLS] is this jack ##son ##ville ? [SEP] no it is not . [SEP]
        #  type_ids: 0   0  0    0    0     0       0 0    1  1  1  1   1 1
        # (b) For single sequences:
        #  tokens:   [CLS] the dog is hairy . [SEP]
        #  type_ids: 0   0   0   0  0     0 0
        #
        # Where "type_ids" are used to indicate whether this is the first
        # sequence or the second sequence. The embedding vectors for `type=0` and
        # `type=1` were learned during pre-training and are added to the wordpiece
        # embedding vector (and position vector). This is not *strictly* necessary
        # since the [SEP] token unambiguously separates the sequences, but it makes
        # it easier for the model to learn the concept of sequences.
        #
        # For classification tasks, the first vector (corresponding to [CLS]) is
        # used as as the "sentence vector". Note that this only makes sense because
        # the entire model is fine-tuned.
        tokens = ["[CLS]"] + tokens_a + ["[SEP]"]
        segment_ids = [0] * len(tokens)

        if tokens_b:
            tokens += tokens_b + ["[SEP]"]
            segment_ids += [1] * (len(tokens_b) + 1)

        input_ids = tokenizer.convert_tokens_to_ids(tokens)

        # The mask has 1 for real tokens and 0 for padding tokens. Only real
        # tokens are attended to.
        input_mask = [1] * len(input_ids)

        # Zero-pad up to the sequence length.
        padding = [0] * (max_seq_length - len(input_ids))
        input_ids += padding
        input_mask += padding
        segment_ids += padding

        assert len(input_ids) == max_seq_length
        assert len(input_mask) == max_seq_length
        assert len(segment_ids) == max_seq_length

        if output_mode == "classification":
            label_id = label_map[example.label]
        elif output_mode == "regression":
            label_id = float(example.label)
        else:
            raise KeyError(output_mode)

        if ex_index < 5:
            logger.info("*** Example ***")
            logger.info("guid: %s" % (example.guid))
            logger.info("tokens: %s" % " ".join(
                [str(x) for x in tokens]))
            logger.info("input_ids: %s" % " ".join([str(x) for x in input_ids]))
            logger.info("input_mask: %s" % " ".join([str(x) for x in input_mask]))
            logger.info(
                "segment_ids: %s" % " ".join([str(x) for x in segment_ids]))
            logger.info("label: %s (id = %d)" % (example.label, label_id))

        features.append(
            InputFeatures(input_ids=input_ids,
                          input_mask=input_mask,
                          segment_ids=segment_ids,
                          label_id=label_id))

        features_by_label[label_id].append(
            InputFeatures(input_ids=input_ids,
                          input_mask=input_mask,
                          segment_ids=segment_ids,
                          label_id=label_id))

    if sep is False:
        return features
    else:
        assert len(features) == (len(features_by_label[0]) + len(features_by_label[1]))
        logger.info(" total:  %d\tlabel 0: %d\tlabel 1: %d " % (
            len(features), len(features_by_label[0]), len(features_by_label[1])))
        return features, features_by_label


def _truncate_seq_pair(tokens_a, tokens_b, max_length):
    """Truncates a sequence pair in place to the maximum length."""

    # This is a simple heuristic which will always truncate the longer sequence
    # one token at a time. This makes more sense than truncating an equal percent
    # of tokens from each, since if one sequence is very short then each token
    # that's truncated likely contains more information than a longer sequence.
    while True:
        total_length = len(tokens_a) + len(tokens_b)
        if total_length <= max_length:
            break
        if len(tokens_a) > len(tokens_b):
            tokens_a.pop()
        else:
            tokens_b.pop()


def simple_accuracy(preds, labels):
    return (preds == labels).mean()


def acc_and_f1(preds, labels):
    acc = simple_accuracy(preds, labels)
    f1 = f1_score(y_true=labels, y_pred=preds)
    return {
        "acc": acc,
        "f1": f1,
        "acc_and_f1": (acc + f1) / 2,
    }


def acc_precision_recall_f1(preds, labels):
    acc = simple_accuracy(preds, labels)
    precision, recall, f1, _ = precision_recall_fscore_support(y_true=labels, y_pred=preds)
    results = {
        "acc": round(acc, 4),
        "precision": [round(x, 4) for x in precision],
        "recall": [round(x, 4) for x in recall],
        "f1": [round(x, 4) for x in f1],
        # "acc_and_f1": (acc + f1) / 2,
    }
    return results


def ranking_metric(preds, labels, probs, ids):
    acc = simple_accuracy(preds, labels)
    precision, recall, f1, _ = precision_recall_fscore_support(y_true=labels, y_pred=preds)
    results = {
        "acc": acc,
        "precision": precision,
        "recall": recall,
        "f1": f1,
        "acc_and_f1": (acc + f1) / 2,
    }
    if ids is None:
        return results
    else:
        r1, r10, r50, mrr = ranking_eval(labels, probs, ids)
        results['R@1'] = r1
        results['R@10'] = r10
        results['R@50'] = r50
        results['MRR'] = mrr
        return results


def pearson_and_spearman(preds, labels):
    pearson_corr = pearsonr(preds, labels)[0]
    spearman_corr = spearmanr(preds, labels)[0]
    return {
        "pearson": pearson_corr,
        "spearmanr": spearman_corr,
        "corr": (pearson_corr + spearman_corr) / 2,
    }


def compute_metrics(task_name, preds, labels, probs=None, ids=None):
    assert len(preds) == len(labels)
    if task_name == "cola":
        return {"mcc": matthews_corrcoef(labels, preds)}
    elif task_name == "sst-2":
        return {"acc": simple_accuracy(preds, labels)}
    elif task_name == "mrpc":
        return acc_and_f1(preds, labels)
    elif task_name == "sts-b":
        return pearson_and_spearman(preds, labels)
    elif task_name == "qqp":
        return acc_and_f1(preds, labels)
    elif task_name == "mnli":
        return {"acc": simple_accuracy(preds, labels)}
    elif task_name == "mnli-mm":
        return {"acc": simple_accuracy(preds, labels)}
    elif task_name == "squad":
        return acc_and_f1(preds, labels)
    elif task_name == "rte":
        return {"acc": simple_accuracy(preds, labels)}
    elif task_name == "wnli":
        return {"acc": simple_accuracy(preds, labels)}
    elif task_name == "semeval":
        return acc_and_f1(preds, labels)
    elif task_name == "quac":
        return acc_precision_recall_f1(preds, labels)
    elif task_name == "dstc":
        return acc_precision_recall_f1(preds, labels, ids)
    elif task_name == "selqa":
        return acc_precision_recall_f1(preds, labels)
    elif task_name == "ubuntu":
        return ranking_metric(preds, labels, probs, ids)
    elif task_name == "cifar-10-bin":
        return acc_precision_recall_f1(preds, labels)
    elif task_name in ["cifar-10", "mnist", "svhn"]:
        return acc_precision_recall_f1(preds, labels)
    else:
        raise KeyError(task_name)


def get_embedding(counter, emb_file=None, size=None, vec_size=None):
    logger.info("Generating word embedding...")
    embedding_dict = {}
    if emb_file is not None:
        assert size is not None
        assert vec_size is not None
        with open(emb_file, "r", encoding="utf-8") as fh:
            for line in tqdm(fh, total=size):
                array = line.split()
                word = "".join(array[0:-vec_size])
                vector = list(map(float, array[-vec_size:]))
                if word in counter:
                    embedding_dict[word] = vector
        logger.info("{} / {} tokens have corresponding word embedding vector".format(
            len(embedding_dict), len(embedding_dict)))
    else:
        assert vec_size is not None
        for token in counter:
            embedding_dict[token] = [np.random.normal(
                scale=0.1) for _ in range(vec_size)]
        logger.info("{} tokens have corresponding embedding vector".format(len(counter)))

    NULL = "--NULL--"
    OOV = "--OOV--"
    token2idx_dict = {token: idx for idx, token in enumerate(embedding_dict.keys(), 2)}
    token2idx_dict[NULL] = 0
    token2idx_dict[OOV] = 1
    embedding_dict[NULL] = [0. for _ in range(vec_size)]
    embedding_dict[OOV] = [0. for _ in range(vec_size)]
    idx2emb_dict = {idx: embedding_dict[token] for token, idx in token2idx_dict.items()}
    emb_mat = [idx2emb_dict[idx] for idx in range(len(idx2emb_dict))]
    return emb_mat, token2idx_dict


def model_loader(args, device, num_labels, pre_trained=False, embeddings=None, bin_file=None):
    if args.model_name.split("-")[0] == 'bert':
        assert args.model_name in ["bert-base-uncased", "bert-large-uncased",
                                   "bert-base-cased", "bert-large-cased",
                                   "bert-base-multilingual-uncased",
                                   "bert-base-multilingual-cased", "bert-base-chinese.",
                                   "bert-base-uncased-cnn"]
        if pre_trained is True:
            model = BertForSequenceClassification.from_pretrained(args.output_dir, num_labels=num_labels)
        else:
            # cache_dir = args.cache_dir if args.cache_dir else os.path.join(str(PYTORCH_PRETRAINED_BERT_CACHE),
            #                                                                'distributed_{}'.format(args.local_rank))
            model = BertForSequenceClassification.from_pretrained(args.model_name, num_labels=num_labels)
    elif args.model_name == 'vgg':
        if args.task_name in ["cifar-10", "svhn"]:
            model = VGG(num_label=num_labels, _in_channels=3)
        elif args.MNIST:
            model = VGG(_type='VGG16-M', num_label=num_labels, _in_channels=1)
        if pre_trained is True:
            if bin_file is None:
                model.load_state_dict(torch.load(os.path.join(args.output_dir, 'pytorch_model.bin')))
            else:
                model.load_state_dict(torch.load(os.path.join(args.output_dir, bin_file)))

    else:
        embed_mat = torch.tensor(embeddings).to(device)
        embedding = nn.Embedding.from_pretrained(embed_mat, freeze=False)
        embedding.embedding_size = 300
        rnn_hidden_size = 600
        model = RNNEncoder('LSTM', bidirectional=True, num_layers=1, hidden_size=rnn_hidden_size, embeddings=embedding)
        if pre_trained is True:
            model.load_state_dict(torch.load(os.path.join(args.output_dir, 'pytorch_model.bin')))
        else:
            model = RNNEncoder('LSTM', bidirectional=True, num_layers=1, hidden_size=rnn_hidden_size, embeddings=embedding)

    return model


def tokenizer_loader(args, device, num_labels, pre_trained=False, embeddings=None):
    if args.BERT:
        assert args.model_name in ["bert-base-uncased", "bert-large-uncased",
                                   "bert-base-cased", "bert-large-cased",
                                   "bert-base-multilingual-uncased",
                                   "bert-base-multilingual-cased", "bert-base-chinese.",
                                   "bert-base-uncased-cnn"]
        if pre_trained is True:
            # model = BertForSequenceClassification.from_pretrained(args.output_dir, num_labels=num_labels)
            tokenizer = BertTokenizer.from_pretrained(args.output_dir, do_lower_case=args.do_lower_case)
        else:
            # cache_dir = args.cache_dir if args.cache_dir else os.path.join(str(PYTORCH_PRETRAINED_BERT_CACHE),
            #                                                                'distributed_{}'.format(args.local_rank))
            # model = BertForSequenceClassification.from_pretrained(args.model_name, cache_dir=cache_dir,
            #                                                       num_labels=num_labels)
            tokenizer = BertTokenizer.from_pretrained(args.model_name, do_lower_case=args.do_lower_case)
    elif args.task_name in ["cifar-10", "mnist", "svhn"]:
        tokenizer = None
    else:
        tokenizer = simple_tokenizer()

    return tokenizer


def main():
    parser = argparse.ArgumentParser()

    ## Required parameters
    parser.add_argument("--data_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The input data dir. Should contain the .tsv files (or other data files) for the task.")
    parser.add_argument("--model_name", default=None, type=str, required=True)
    parser.add_argument("--bert_model", default=None, type=str, required=False,
                        help="Bert pre-trained model selected in the list: bert-base-uncased, "
                             "bert-large-uncased, bert-base-cased, bert-large-cased, bert-base-multilingual-uncased, "
                             "bert-base-multilingual-cased, bert-base-chinese.")
    parser.add_argument("--task_name",
                        default=None,
                        type=str,
                        required=True,
                        help="The name of the task to train.")
    parser.add_argument("--output_dir",
                        default=None,
                        type=str,
                        required=True,
                        help="The output directory where the model predictions and checkpoints will be written.")

    ## Other parameters
    parser.add_argument("--cache_dir",
                        default="",
                        type=str,
                        help="Where do you want to store the pre-trained models downloaded from s3")
    parser.add_argument("--max_seq_length",
                        default=128,
                        type=int,
                        help="The maximum total input sequence length after WordPiece tokenization. \n"
                             "Sequences longer than this will be truncated, and sequences shorter \n"
                             "than this will be padded.")
    parser.add_argument("--do_train",
                        action='store_true',
                        help="Whether to run training.")
    parser.add_argument("--do_eval",
                        action='store_true',
                        help="Whether to run eval on the dev set.")
    parser.add_argument("--do_train_eval",
                        action='store_true',
                        help="Whether to run train and eval at the same time.")
    parser.add_argument("--do_lower_case",
                        action='store_true',
                        help="Set this flag if you are using an uncased model.")
    parser.add_argument("--train_batch_size",
                        default=32,
                        type=int,
                        help="Total batch size for training.")
    parser.add_argument("--eval_batch_size",
                        default=128,
                        type=int,
                        help="Total batch size for eval.")
    parser.add_argument("--learning_rate",
                        default=5e-5,
                        type=float,
                        help="The initial learning rate for Adam.")
    parser.add_argument("--num_train_epochs",
                        default=3.0,
                        type=float,
                        help="Total number of training epochs to perform.")
    parser.add_argument("--warmup_proportion",
                        default=0.1,
                        type=float,
                        help="Proportion of training to perform linear learning rate warmup for. "
                             "E.g., 0.1 = 10%% of training.")
    parser.add_argument("--no_cuda",
                        action='store_true',
                        help="Whether not to use CUDA when available")
    parser.add_argument("--local_rank",
                        type=int,
                        default=-1,
                        help="local_rank for distributed training on gpus")
    parser.add_argument('--seed',
                        type=int,
                        default=42,
                        help="random seed for initialization")
    parser.add_argument('--gradient_accumulation_steps',
                        type=int,
                        default=1,
                        help="Number of updates steps to accumulate before performing a backward/update pass.")
    parser.add_argument('--fp16',
                        action='store_true',
                        help="Whether to use 16-bit float precision instead of 32-bit")
    parser.add_argument('--loss_scale',
                        type=float, default=0,
                        help="Loss scaling to improve fp16 numeric stability. Only used when fp16 set to True.\n"
                             "0 (default value): dynamic loss scaling.\n"
                             "Positive power of 2: static loss scaling value.\n")
    parser.add_argument('--do_sampling', type=bool, default=False)
    parser.add_argument('--sampling_method', type=str, default='random', choices=['random', 'weighted', 'top-k', 'border', 'tardy'])
    parser.add_argument('--do_histloss', type=bool, default=False)
    parser.add_argument('--debug', type=bool, default=False)
    parser.add_argument('--KLD_rg', type=bool, default=False)
    parser.add_argument('--mu_rg', type=bool, default=False)
    # parser.add_argument('--sampling_size', type=int, default=5000)
    parser.add_argument('--negative_size', type=int, default=0, help="sampling size for major class")
    parser.add_argument('--positive_size', type=int, default=0, help="size of minor class")
    # parser.add_argument('--pre_trained_model', type=str, default='', help='pre-trained model for eval')
    parser.add_argument('--BERT', type=bool, default=False)
    parser.add_argument('--CIFAR', type=bool, default=False)
    parser.add_argument('--MNIST', type=bool, default=False)
    parser.add_argument('--tb_log_dir', type=str, default='runs')
    parser.add_argument('--lambda_decay', type=str, default='none', choices=['none', 'exp', 'step'])
    args = parser.parse_args()

    processors = {
        # "cola": ColaProcessor,
        # "mnli": MnliProcessor,
        # "mnli-mm": MnliMismatchedProcessor,
        # "mrpc": MrpcProcessor,
        # "sst-2": Sst2Processor,
        # "sts-b": StsbProcessor,
        "qqp": QqpProcessor,
        "squad": QnliProcessor,
        # "rte": RteProcessor,
        # "wnli": WnliProcessor,
        "wikiqa": WikiQAProcessor,
        "semeval": SemevalProcessor,
        "quac": QuacProcessor,
        "dstc": DSTCProcessor,
        "ubuntu": UbuntuProcessor,
        "selqa": SelQAProcessor,
        "cifar-10-bin": CIFAR10BinaryProcessor,
        "cifar-10": CIFAR10Processor,
        "mnist": MNISTProcessor,
        "svhn": SVHNProcessor,
    }

    output_modes = {
        "cola": "classification",
        "mnli": "classification",
        "mrpc": "classification",
        "sst-2": "classification",
        "sts-b": "regression",
        "qqp": "classification",
        "squad": "classification",
        "rte": "classification",
        "wnli": "classification",
        "wikiqa": "classification",
        "semeval": "classification",
        "quac": "classification",
        "dstc": "classification",
        "ubuntu": "classification",
        "selqa": "classification",
        "cifar-10-bin": "classification",
        "cifar-10": "classification",
        "mnist": "classification",
        "svhn": "classification",
    }

    if args.local_rank == -1 or args.no_cuda:
        device = torch.device("cuda" if torch.cuda.is_available() and not args.no_cuda else "cpu")
        n_gpu = torch.cuda.device_count()
    else:
        torch.cuda.set_device(args.local_rank)
        device = torch.device("cuda", args.local_rank)
        n_gpu = 1
        # Initializes the distributed backend which will take care of sychronizing nodes/GPUs
        torch.distributed.init_process_group(backend='nccl')

    logging.basicConfig(format='%(asctime)s - %(levelname)s - %(name)s -   %(message)s',
                        datefmt='%m/%d/%Y %H:%M:%S',
                        level=logging.INFO if args.local_rank in [-1, 0] else logging.WARN)

    logger.info("device: {} n_gpu: {}, distributed training: {}, 16-bits training: {}".format(
        device, n_gpu, bool(args.local_rank != -1), args.fp16))

    if args.gradient_accumulation_steps < 1:
        raise ValueError("Invalid gradient_accumulation_steps parameter: {}, should be >= 1".format(
            args.gradient_accumulation_steps))

    args.train_batch_size = args.train_batch_size // args.gradient_accumulation_steps

    random.seed(args.seed)
    np.random.seed(args.seed)
    torch.manual_seed(args.seed)
    if n_gpu > 0:
        torch.cuda.manual_seed_all(args.seed)

    if not args.do_train and not args.do_eval:
        raise ValueError("At least one of `do_train` or `do_eval` must be True.")

    # if os.path.exists(args.output_dir) and os.listdir(args.output_dir) and args.do_train:
    #     raise ValueError("Output directory ({}) already exists and is not empty.".format(args.output_dir))
    if not os.path.exists(args.output_dir):
        os.makedirs(args.output_dir)

    task_name = args.task_name.lower()

    if task_name not in processors:
        raise ValueError("Task not found: %s" % task_name)

    processor = processors[task_name]()
    output_mode = output_modes[task_name]

    label_list = processor.get_labels()
    num_labels = len(label_list)
    word_emb_mat = None
    args.BERT = True if args.model_name.split("-")[0] == "bert" else False
    args.CIFAR = True if args.task_name.split("-")[0] == "cifar" else False
    args.MNIST = True if args.task_name == "mnist" else False

    summary = SummaryWriter(log_dir=args.tb_log_dir)  # default 'log_dir' is "runs"

    if args.do_train:
        # Prepare tokenizer
        tokenizer = tokenizer_loader(args, device, num_labels=num_labels)

        features_by_label = ""

        if args.model_name in ["rnn"]:
            word2idx_dict, word_emb_mat = word_embeddings(args, processor, tokenizer, word_emb_mat)

        if args.do_sampling is True:  # for num_train_optimization step
            train_steps_per_ep = math.ceil(
                (args.negative_size + args.positive_size) / args.train_batch_size)  # ceiling
            train_examples = processor.get_train_examples(args.data_dir)
            if args.BERT:
                train_features, features_by_label = convert_examples_to_features(
                    train_examples, label_list, args.max_seq_length, tokenizer, output_mode, sep=True)
            elif args.task_name in ["cifar-10", "mnist", "svhn"]:
                train_vectors, train_labels = train_examples  # (vector, label)
                train_features, features_by_label = divide_features_by_label(train_vectors, train_labels)
            else:
                train_features, features_by_label = convert_examples_to_features_rnn(word2idx_dict,
                    train_examples, label_list, args.max_seq_length, tokenizer, output_mode, sep=True)

            num_train_examples = args.negative_size + args.positive_size
        else:
            if os.path.exists(os.path.join(args.data_dir, 'train-%s.pt' % args.model_name)):
                train_data = torch.load(os.path.join(args.data_dir, 'train-%s.pt' % args.model_name))
                logger.info("load %s" % os.path.join(args.data_dir, 'train-%s.pt' % args.model_name))
            else:
                if args.BERT:
                    train_examples = processor.get_train_examples(args.data_dir)
                    train_features, features_by_label = convert_examples_to_features(
                        train_examples, label_list, args.max_seq_length, tokenizer, output_mode, sep=True)
                    train_data, _ = get_tensor_dataset(args, train_features, output_mode)
                    torch.save(train_data, os.path.join(args.data_dir, 'train-%s.pt' % args.model_name))
                    # torch.save(all_label_ids, os.path.join(args.data_dir, 'train_labels.pt'))
                    logger.info("train data tensors saved !")
                elif args.task_name in ["cifar-10", "mnist", "svhn"]:
                    if args.task_name == 'svhn':
                        processor.adjust_dataset()
                    train_features, labels = processor.get_train_examples(args.data_dir)  # (vector, label)
                    train_data = TensorDataset(
                        torch.tensor(train_features, dtype=torch.float), torch.tensor(labels, dtype=torch.long))
                    if args.task_name == "svhn":
                        torch.save(train_data, os.path.join(args.data_dir, 'train-%s.pt' % args.model_name))
                        logger.info("train data tensors saved !")
                else:
                    train_examples = processor.get_train_examples(args.data_dir)
                    train_features, features_by_label = convert_examples_to_features_rnn(
                        word2idx_dict, train_examples, label_list, args.max_seq_length, tokenizer, output_mode, sep=True)
                    train_data, _ = get_tensor_dataset(args, train_features, output_mode)
                    torch.save(train_data, os.path.join(args.data_dir, 'train-%s.pt' % args.model_name))
                    # torch.save(all_label_ids, os.path.join(args.data_dir, 'train_labels.pt'))
                    logger.info("train data tensors saved !")

            num_train_examples = len(train_data)
            if args.local_rank == -1:
                _train_sampler = RandomSampler(train_data)
            else:
                _train_sampler = DistributedSampler(train_data)
            train_dataloader = DataLoader(train_data, sampler=_train_sampler, batch_size=args.train_batch_size)
            train_steps_per_ep = len(train_dataloader)

        # Prepare data for devset
        if os.path.exists(os.path.join(args.data_dir, 'dev-%s.pt' % args.model_name)):
            dev_data = torch.load(os.path.join(args.data_dir, 'dev-%s.pt' % args.model_name))
            all_dev_label_ids = torch.load(os.path.join(args.data_dir, 'dev_labels.pt'))
            logger.info("load %s" % os.path.join(args.data_dir, 'dev-%s.pt' % args.model_name))
        else:
            if args.BERT:
                dev_examples = processor.get_dev_examples(args.data_dir)
                dev_features = convert_examples_to_features(
                    dev_examples, label_list, args.max_seq_length, tokenizer, output_mode)
                dev_data, all_dev_label_ids = get_tensor_dataset(args, dev_features, output_mode)
            elif args.task_name in ["cifar-10", "mnist", "svhn"]:
                dev_data, dev_labels = processor.get_dev_examples(args.data_dir)
                all_dev_label_ids = torch.tensor(dev_labels, dtype=torch.long)
                dev_data = TensorDataset(torch.tensor(dev_data, dtype=torch.float), all_dev_label_ids)
            else:
                dev_examples = processor.get_dev_examples(args.data_dir)
                dev_features = convert_examples_to_features_rnn(
                    word2idx_dict, dev_examples, label_list, args.max_seq_length, tokenizer, output_mode)
                dev_data, all_dev_label_ids = get_tensor_dataset(args, dev_features, output_mode)
            torch.save(dev_data, os.path.join(args.data_dir, 'dev-%s.pt' % args.model_name))
            torch.save(all_dev_label_ids, os.path.join(args.data_dir, 'dev_labels.pt'))
            logger.info("dev data tensors saved !")

        num_train_optimization_steps = train_steps_per_ep // args.gradient_accumulation_steps * args.num_train_epochs
        if args.local_rank != -1:
            num_train_optimization_steps = num_train_optimization_steps // torch.distributed.get_world_size()

        model = model_loader(args, device, num_labels=num_labels, embeddings=word_emb_mat)
        model.to(device)
        if n_gpu > 1:
            model = torch.nn.DataParallel(model)

        # Prepare optimizer

        param_optimizer = list(model.named_parameters())
        no_decay = ['bias', 'LayerNorm.bias', 'LayerNorm.weight']
        optimizer_grouped_parameters = [
            {'params': [p for n, p in param_optimizer if not any(nd in n for nd in no_decay)], 'weight_decay': 0.01},
            {'params': [p for n, p in param_optimizer if any(nd in n for nd in no_decay)], 'weight_decay': 0.0}
        ]

        # loss weight
        _lambda = torch.nn.Parameter(torch.tensor([0.0001], dtype=torch.float).to(device))

        optimizer_grouped_parameters[1]['params'].append(_lambda)
        optimizer = AdamW(optimizer_grouped_parameters, lr=args.learning_rate)
        # optimizer_grouped_parameters2 = [
        #     {'params': [p for n, p in param_optimizer if n in ["classifier.weight", "classifier.bias"]],
        #      'weight_decay': 0.01}
        # ]
        # optimizer2 = AdamW(optimizer_grouped_parameters2, lr=args.learning_rate)

        global_step = 0
        tr_loss = 0

        logger.info("***** Running training *****")
        logger.info("  Num examples = %d", num_train_examples)
        logger.info("  Batch size = %d", args.train_batch_size)
        logger.info("  Num steps = %d", num_train_optimization_steps)

        dev_results = []
        var_results = []
        mean_results = []
        for ep in range(1, int(args.num_train_epochs) + 1):
            model.train()
            nb_tr_examples = 0

            if args.do_sampling is True:
                logger.info(" [epoch %d] (sampling) get new dataloader ... " % ep)
                train_dataloader = get_sampling_dataloader(ep, args, features_by_label, BERT)

            logger.info(" [epoch %d] trainig iteration starts ... *****" % ep)
            t_prob = []
            c_prob = [[] for _ in range(num_labels)]
            d_prob = [[] for _ in range(num_labels)]
            for step, batch in enumerate(train_dataloader):
                batch = tuple(t.to(device) for t in batch)

                if args.BERT:
                    input_ids, input_mask, segment_ids, label_ids, preprob = batch
                    # define a new function to compute loss values for both output_modes
                    outputs = model(input_ids, segment_ids, input_mask, labels=None)
                    logits = outputs[0]  # if labels is None, outputs[0] is logits
                elif args.task_name in ["cifar-10", "mnist", "svhn"]:
                    inputs, label_ids = batch
                    logits = model(inputs)
                else:
                    input_ids_a, input_ids_b, label_ids, preprob = batch
                    logits = model(input_ids_a, input_ids_b)
                loss_fct = CrossEntropyLoss(reduction='none')
                _loss = loss_fct(logits.view(-1, num_labels), label_ids.view(-1))
                loss1 = _loss.mean()  # default = 'mean'
                summary.add_scalar('training_loss', loss1.item(), global_step)

                # label_mask = (label_ids == 1)  # major class == 0
                # major_probs = Softmax(dim=1)(logits[label_mask])[:, 1]
                # minor_probs = Softmax(dim=1)(logits[~label_mask])[:, 1]
                # n_minor = minor_probs.size(0)
                loss = loss1

                if args.debug is True or args.KLD_rg is True or args.mu_rg is True:
                    # major_t_prob.extend(major_probs.tolist())
                    # minor_t_prob.extend(minor_probs.tolist())
                    # probs = Softmax(dim=1)(logits)[:, 1]
                    probs = Softmax(dim=1)(logits)
                    t_prob.extend(probs.view(-1).tolist())
                    for k in range(num_labels):
                        d_prob[k].extend(probs[:, k].tolist())
                        _mask = (label_ids == k)
                        if _mask.size(0) > 0:
                            _probs = Softmax(dim=1)(logits[_mask])[:, k]
                            c_prob[k].extend(_probs.tolist())

                if n_gpu > 1:
                    loss = loss.mean()  # mean() to average on multi-gpu.
                if args.gradient_accumulation_steps > 1:
                    loss = loss / args.gradient_accumulation_steps

                tr_loss += loss.item()
                nb_tr_examples += label_ids.size(0)
                global_step += 1
                # if global_step % 2000:
                #     logger.info("   loss = %.8f" % loss)

                # experiment: minimize the difference between two classes
                min_probs = []
                max_probs = []
                mu_sum = 0
                if args.KLD_rg is True or args.mu_rg is True:
                    temp = []
                    for k in range(num_labels):
                        temp.append(probs[:, k].var())
                        if args.mu_rg is True:
                            mu_sum += abs(1 / num_labels - probs[:, k].mean())
                    if args.KLD_rg is True:
                        _max_var_idx = np.array(temp).argmax()
                        _min_var_idx = np.array(temp).argmin()
                        max_probs = probs[:, _max_var_idx]
                        min_probs = probs[:, _min_var_idx]
                if args.KLD_rg is True:  # exclude the case only negatives in mini-batch.
                    _lambda_var = 0.001
                    # TODO compare KLD value with baseline and regularized model.
                    # _lambda_var = 0
                    if args.lambda_decay == 'none':
                        _decay = 1
                    elif args.lambda_decay == 'exp':
                        _k = 0.01
                        _decay = math.exp(-_k * ep)  # decay = lambda * exp(-kt)
                    elif args.lambda_decay == 'step':
                        step_size = 20
                        _decay = (1 - 0.1 * math.floor(ep/step_size))
                    KLD1 = KL_loss(min_probs, max_probs)
                    KLD2 = KL_loss(max_probs, min_probs)
                    kld = 0.5 * (KLD1 + KLD2)
                    summary.add_scalar('KLD', kld, global_step)
                    if kld < 20:
                        loss = loss + _lambda_var * _decay * kld
                if args.mu_rg is True:
                    _lambda_mu = 0.0001
                    if args.lambda_decay == 'none':
                        _decay = 1
                    elif args.lambda_decay == 'exp':
                        _k = 0.01
                        _decay = math.exp(-_k * ep)  # decay = lambda * exp(-kt)
                    elif args.lambda_decay == 'step':
                        step_size = 20
                        _decay = (1 - 0.1 * math.floor(ep/step_size))
                    mu = mu_sum / num_labels
                    summary.add_scalar('mu', mu, global_step)
                    loss = loss + _lambda_mu * _decay * mu

                loss.backward(retain_graph=True)
                optimizer.step()
                optimizer.zero_grad()
                _lambda = torch.clamp(_lambda, min=0.00001, max=0.001)

            # end of epoch
            if args.KLD_rg is True:
                logger.info("KL Divergence Regularization applied with %s | decay: %s" % (str(_lambda_var), args.lambda_decay))
            if args.mu_rg is True:
                logger.info("Mean Divergence Regularization applied with %s | decay: %s" % (str(_lambda_mu), args.lambda_decay))
            if args.debug is True:
                pass
                # logger.info("prob mean: %.6f, var: %.6f" %
                #             (torch.tensor(t_prob).mean().item(),
                #              torch.tensor(t_prob).var().item()))
                # logger.info("     weight: %0.6f" % model.layer_norm.weight[0])
                # logger.info("       bias: %0.6f" % model.layer_norm.bias[0])
                # for t in model.named_parameters():
                #     name, param = t
                #     logger.info("%20s, mean: %0.6f, var: %0.6f" % (name, param.mean(), param.var()))
                # sampling_ploting(t_prob, ep)
                # output_ploting(label0_t_prob, label1_t_prob, ep)
                # mean_results.append(torch.tensor(t_prob).mean().item())
                # var_results.append(torch.tensor(t_prob).var().item())
                # for k in range(num_labels):
                #     logger.info("[class %d (y=true)] %d mean: %.4f, var: %.4f" % (
                #         k, len(c_prob[k]), torch.tensor(c_prob[k]).mean().item(), torch.tensor(c_prob[k]).var().item()))
                # for k in range(num_labels):
                #     logger.info("[class %d (total )] %d mean: %.4f, var: %.4f" % (
                #         k, len(d_prob[k]), torch.tensor(d_prob[k]).mean().item(), torch.tensor(d_prob[k]).var().item()))

            ##########################################################################
            # update weight in sampling experiments
            if args.do_sampling is True and args.sampling_method not in ['random']:
                logger.info(" [epoch %d] update pre probs ... " % ep)
                train_features, features_by_label = update_probs(ep, train_features, model, device, args, BERT, CIFAR)
            ##########################################################################
            # eval with dev set.
            dev_sampler = SequentialSampler(dev_data)
            # dev_sampler = RandomSampler(dev_data, replacement=False)
            if task_name == 'wikiqa':
                dev_dataloader = DataLoader(dev_data, sampler=dev_sampler, batch_size=1)
                score, log = wikiqa_eval(ep, device, dev_examples, dev_dataloader, model, logger, BERT)
                score = round(score, 4)
                dev_results.append(score)
            elif task_name == 'semeval':
                dev_dataloader = DataLoader(dev_data, sampler=dev_sampler, batch_size=1)
                score = semeval_eval(ep, device, dev_examples, dev_dataloader, model, logger, BERT, _type="dev")
                score = round(score, 4)
            else:
                dev_dataloader = DataLoader(dev_data, sampler=dev_sampler, batch_size=args.eval_batch_size)

                model.eval()
                dev_loss = 0
                nb_dev_steps = 0
                preds = []

                true_dist = np.zeros(10)
                pred_dist = np.zeros(10)
                logger.info(" [epoch %d] devset evaluating ... " % ep)
                for idx, batch in enumerate(dev_dataloader):
                    batch = tuple(t.to(device) for t in batch)
                    with torch.no_grad():
                        if args.BERT:
                            input_ids, input_mask, segment_ids, label_ids, preprob = batch
                            logits, _, _ = model(input_ids, segment_ids, input_mask, labels=None)
                        elif args.task_name in ["cifar-10", "mnist", "svhn"]:
                            inputs, label_ids = batch
                            logits = model(inputs)
                        else:
                            input_ids_a, input_ids_b, label_ids, preprob = batch
                            logits = model(input_ids_a, input_ids_b)

                    # create eval loss and other metric required by the task
                    if output_mode == "classification":
                        loss_fct = CrossEntropyLoss()
                        tmp_eval_loss = loss_fct(logits.view(-1, num_labels), label_ids.view(-1))
                    elif output_mode == "regression":
                        loss_fct = MSELoss()
                        tmp_eval_loss = loss_fct(logits.view(-1), label_ids.view(-1))
                    dev_loss += tmp_eval_loss.mean().item()
                    nb_dev_steps += 1
                    _probs = Softmax(dim=1)(logits)
                    if len(preds) == 0:
                        preds.append(logits.detach().cpu().numpy())
                        probs = _probs.tolist()
                    else:
                        preds[0] = np.append(preds[0], logits.detach().cpu().numpy(), axis=0)
                        probs.extend(_probs.tolist())
                    # for j in label_ids:
                    #     true_dist[j] += 1
                    # pred_dist += _probs.sum(dim=0).tolist()
                        # summary.add_histogram('output_distributions', _probs.mean(dim=0), global_step=ep)

                # true_dist = true_dist / np.sum(true_dist)
                # pred_dist = pred_dist / len(dev_data)
                # summary.add_figure('output_distributions',
                #                    # plot_vector_bar(true_dist, _probs.mean(dim=0).tolist(), processor.get_class_name()),
                #                    plot_vector_bar(true_dist, pred_dist, processor.get_class_name()),
                #                    global_step=ep)
                # if ep == 37:
                #     summary.add_figure('output_distributions',
                #                        # plot_vector_bar(true_dist, _probs.mean(dim=0).tolist(), processor.get_class_name()),
                #                        plot_vector_bar(true_dist, pred_dist, processor.get_class_name()),
                #                        global_step=ep)
                dev_loss = dev_loss / nb_dev_steps
                preds = preds[0]
                if output_mode == "classification":
                    preds = np.argmax(preds, axis=1)
                elif output_mode == "regression":
                    preds = np.squeeze(preds)
                result = compute_metrics(task_name, preds, all_dev_label_ids.numpy(), probs=probs)
                loss = tr_loss / global_step if args.do_train else None

                result['dev_loss'] = dev_loss
                result['loss'] = loss
                logger.info(" [epoch %d] devset eval results " % ep)
                for key in sorted(result.keys()):
                    logger.info("  %s = %s", key, str(result[key]))

                if task_name == "squad":
                    score = round(result['f1'], 4)
                    dev_results.append(score)
                elif task_name in ["quac", "dstc", 'ubuntu', 'selqa', 'cifar-10-bin']:
                    score = round(result['f1'][1], 4)
                    dev_results.append(score)
                elif task_name in ["cifar-10", "mnist"]:
                    score = round(result['acc'], 4)
                    dev_results.append(score)
                    major_f1 = result['f1'][0]
                    minor_f1 = np.array(result['f1'][1:]).mean()
                    macro_f1 = np.array(result['f1']).mean()
                    logger.info("  macro f1       = %.4f (macro avg of f1)", macro_f1)
                    logger.info("  major f1       = %.4f", major_f1)
                    logger.info("  minor f1 (avg) = %.4f", minor_f1)
                    summary.add_scalar('major_f1', major_f1, ep)
                    summary.add_scalar('minor_f1', minor_f1, ep)
                else:
                    score = round(result['acc'], 4)
                    dev_results.append(score)
            summary.add_scalar('dev_score', score, ep)
            model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
            output_model_file = os.path.join(args.output_dir, 'pytorch_model_%d_%.4f.bin' % (ep, score))
            torch.save(model_to_save.state_dict(), output_model_file)

        # end of whole training
        idx, _max = 0, 0
        for i, result in enumerate(dev_results):
            if result > _max:
                _max = result
                _idx = i+1
            print(result)
        print("max: ep %d, %.4f" % (_idx, _max))
        max_model_file = ("pytorch_model_%d_%.4f.bin" % (_idx, _max))
        print("max weight: %s" % max_model_file)
        print("mean")
        for result in mean_results:
            print(result)
        print("var")
        for result in var_results:
            print(result)

    if args.do_train and (args.local_rank == -1 or torch.distributed.get_rank() == 0):
        output_model_file = os.path.join(args.output_dir, WEIGHTS_NAME)
        # output_config_file = os.path.join(args.output_dir, CONFIG_NAME)

        if args.BERT:
            model_to_save = model.module if hasattr(model, 'module') else model  # Only save the model it-self
            torch.save(model_to_save.state_dict(), output_model_file)
            # model_to_save.config.to_json_file(output_config_file)
            tokenizer.save_vocabulary(args.output_dir)
        else:
            torch.save(model.state_dict(), output_model_file)

    if args.do_eval or args.do_train_eval:
        if args.model_name == 'rnn':
            if os.path.exists(os.path.join(args.data_dir, 'word_emb_mat.json')):
                with open(args.data_dir + "word_emb_mat.json") as fh:
                    word_emb_mat = json.load(fh)
                with open(args.data_dir+"word2idx.json") as fh:
                    word2idx_dict = json.load(fh)
        tokenizer = tokenizer_loader(args, device, num_labels=num_labels, pre_trained=True)
        if args.do_train_eval:
            logger.info("  Test with %s file" % max_model_file)
            model = model_loader(
                args, device, num_labels=num_labels, pre_trained=True, embeddings=word_emb_mat, bin_file=max_model_file)
        else:
            model = model_loader(args, device, num_labels=num_labels, pre_trained=True, embeddings=word_emb_mat)
        model.to(device)

        # test_ids = [e.guid for e in test_examples]
        if os.path.exists(os.path.join(args.data_dir, 'test-%s.pt' % args.model_name)):
            test_data = torch.load(os.path.join(args.data_dir, 'test-%s.pt' % args.model_name))
            all_label_ids = torch.load(os.path.join(args.data_dir, 'test_labels.pt'))
            logger.info("load %s" % os.path.join(args.data_dir, 'test-%s.pt'))
            if task_name in ['wikiqa', 'semeval']:
                test_examples = processor.get_test_examples(args.data_dir)
        else:
            test_examples = processor.get_test_examples(args.data_dir)
            if args.BERT:
                test_features = convert_examples_to_features(
                    test_examples, label_list, args.max_seq_length, tokenizer, output_mode)
                test_data, all_label_ids = get_tensor_dataset(args, test_features, output_mode)
            elif args.task_name in ["cifar-10", "mnist", "svhn"]:
                test_features, test_labels = test_examples
                all_label_ids = torch.tensor(test_labels, dtype=torch.long)
                test_data = TensorDataset(
                    torch.tensor(test_features, dtype=torch.float), all_label_ids)
            else:
                test_features = convert_examples_to_features_rnn(
                    word2idx_dict, test_examples, label_list, args.max_seq_length, tokenizer, output_mode)
                test_data, all_label_ids = get_tensor_dataset(args, test_features, output_mode)
            torch.save(test_data, os.path.join(args.data_dir, 'test-%s.pt' % args.model_name))
            torch.save(all_label_ids, os.path.join(args.data_dir, 'test_labels.pt'))
            logger.info("Test data tensors saved !")

        logger.info("***** Running evaluation *****")
        logger.info("  Num examples = %d", len(test_data))
        logger.info("  Batch size = %d", args.eval_batch_size)

        # Run prediction for full data
        eval_sampler = SequentialSampler(test_data)
        if task_name == 'wikiqa':
            eval_dataloader = DataLoader(test_data, sampler=eval_sampler, batch_size=1)
            _ = wikiqa_eval(0, device, test_examples, eval_dataloader, model, logger, BERT)
        elif task_name == 'semeval':
            eval_dataloader = DataLoader(test_data, sampler=eval_sampler, batch_size=1)
            _ = semeval_eval(0, device, test_examples, eval_dataloader, model, logger, BERT, _type="test")
        else:
            eval_dataloader = DataLoader(test_data, sampler=eval_sampler, batch_size=args.eval_batch_size)

            model.eval()
            eval_loss = 0
            nb_eval_steps = 0
            preds = []
            probs = []

            for batch in tqdm(eval_dataloader, desc="Evaluating"):
                batch = tuple(t.to(device) for t in batch)
                with torch.no_grad():
                    if args.BERT:
                        input_ids, input_mask, segment_ids, label_ids, preprob = batch
                        logits, _, _ = model(input_ids, segment_ids, input_mask, labels=None)
                    elif args.task_name in ["cifar-10", "mnist", "svhn"]:
                        inputs, label_ids = batch
                        logits = model(inputs)
                    else:
                        input_ids_a, input_ids_b, label_ids, preprob = batch
                        logits = model(input_ids_a, input_ids_b)

                # create eval loss and other metric required by the task
                if output_mode == "classification":
                    loss_fct = CrossEntropyLoss()
                    tmp_eval_loss = loss_fct(logits.view(-1, num_labels), label_ids.view(-1))
                elif output_mode == "regression":
                    loss_fct = MSELoss()
                    tmp_eval_loss = loss_fct(logits.view(-1), label_ids.view(-1))

                eval_loss += tmp_eval_loss.mean().item()
                nb_eval_steps += 1
                if len(preds) == 0:
                    preds.append(logits.detach().cpu().numpy())
                    probs = Softmax(dim=1)(logits).tolist()
                else:
                    preds[0] = np.append(preds[0], logits.detach().cpu().numpy(), axis=0)
                    probs.extend(Softmax(dim=1)(logits).tolist())

            eval_loss = eval_loss / nb_eval_steps
            preds = preds[0]
            if output_mode == "classification":
                preds = np.argmax(preds, axis=1)
            elif output_mode == "regression":
                preds = np.squeeze(preds)
            result = compute_metrics(task_name, preds, all_label_ids.numpy(),
                                     probs=probs)
            loss = tr_loss / global_step if args.do_train else None

            result['eval_loss'] = eval_loss
            # result['global_step'] = global_step
            result['loss'] = loss

            output_eval_file = os.path.join(args.output_dir, "eval_results.txt")
            with open(output_eval_file, "w") as writer:
                logger.info("***** Eval results *****")
                for key in sorted(result.keys()):
                    logger.info("  %s = %s", key, str(result[key]))
                    writer.write("%s = %s\n" % (key, str(result[key])))
                macro_f1 = np.array(result['f1']).mean()
                logger.info("  macro f1       = %.4f (macro avg of f1)", macro_f1)
                logger.info("  major f1       = %.4f", result['f1'][0])
                logger.info("  minor f1 (avg) = %.4f", np.array(result['f1'][1:]).mean())


def word_embeddings(args, processor, tokenizer, word_emb_mat):
    if os.path.exists(os.path.join(args.data_dir, 'word_emb_mat.json')):
        with open(args.data_dir + "word_emb_mat.json") as fh:
            word_emb_mat = json.load(fh)
        with open(args.data_dir + "word2idx.json") as fh:
            word2idx_dict = json.load(fh)
    else:
        counter = Counter()
        word_counting(args, counter, processor, tokenizer)

        word_emb_mat, word2idx_dict = get_embedding(
            counter, '/home/nlpgpu5/data/embeddings/glove.840B.300d.txt', int(2.2e6), 300)
        save(args.data_dir + "word_emb_mat.json", word_emb_mat, message="word embedding")
        save(args.data_dir + "word2idx.json", word2idx_dict, message="word2idx")
    return word2idx_dict, word_emb_mat


def save(filename, obj, message=None):
    if message is not None:
        logger.info("Saving {} {}...".format(len(obj), message))
        with open(filename, "w") as fh:
            json.dump(obj, fh)


def word_counting(args, counter, processor, tokenizer):
    trains = processor.get_train_examples(args.data_dir)
    devs = processor.get_dev_examples(args.data_dir)
    tests = processor.get_test_examples(args.data_dir)
    for (ex_index, example) in enumerate(trains + devs + tests):
        tokens_a = tokenizer.tokenize(example.text_a)
        tokens_b = tokenizer.tokenize(example.text_b)

        for t in (tokens_a + tokens_b):
            counter[t] += 1


def update_probs(ep, train_features, model, device, args):
    def func(x):
        return 4 * (-(x * x) + x)

    train_data, _ = get_tensor_dataset(args, train_features, "classification")
    loader = DataLoader(train_data, sampler=SequentialSampler(train_data), batch_size=1024)
    global_logit_idx = 0
    for batch in loader:
        batch = tuple(t.to(device) for t in batch)

        with torch.no_grad():
            if args.BERT:
                input_ids, input_mask, segment_ids, label_ids, preprob = batch
                # define a new function to compute loss values for both output_modes
                logits, _, _ = model(input_ids, segment_ids, input_mask, labels=None)
            elif args.task_name in ["cifar-10", "mnist", "svhn"]:
                inputs, label_ids = batch
                logits = model(inputs)
            else:
                input_ids_a, input_ids_b, label_ids, preprob = batch
                logits = model(input_ids_a, input_ids_b)

        probs = Softmax(dim=-1)(logits)
        batch_size = logits.size(0)
        for i in range(batch_size):
            if args.sampling_method == 'weighted':
                if label_ids[i] == 0:
                    train_features[global_logit_idx + i].weight = probs[i][1].item()
                else:  # label_ids[i] == 1
                    train_features[global_logit_idx + i].weight = probs[i][0].item()
            elif args.sampling_method == 'border':
                train_features[global_logit_idx+i].weight = func(probs[i][1].item())
            elif args.sampling_method == 'tardy' and ep > 1:
                # TODO tardy sampling
                cur_prob = probs[i][1].item()
                pre_prob = train_features[global_logit_idx+i].preprob
                train_features[global_logit_idx+i].weight = (1 - ((pre_prob-cur_prob)/pre_prob))*cur_prob
            # update pre-prob
            train_features[global_logit_idx + i].preprob = probs[i][1].item()
        global_logit_idx += batch_size

    assert global_logit_idx == len(train_data)

    features_by_label = [[], []]
    for f in train_features:
        if f.label_id == 0:
            features_by_label[0].append(f)
        else:
            features_by_label[1].append(f)
    return train_features, features_by_label


def softmax(x):
    return np.exp(x) / sum(np.exp(x))


def get_sampling_dataloader(ep, args, features_by_label):
    if args.sampling_method not in ['random']:
        # temp = np.array([i.weight for i in features_by_label[0]])
        # weight0 = temp / np.sum(temp)
        weight0 = softmax([i.weight for i in features_by_label[0]])
        weight1 = softmax([i.weight for i in features_by_label[1]])
    # if len(features_by_label[0]) > len(features_by_label[1]):
    if args.sampling_method in ['weighted', 'border'] or (args.sampling_method == 'tardy' and ep > 2):  # weighted sampling
        logger.info(" => %s sampling ..." % args.sampling_method)
        label_0 = np.random.choice(features_by_label[0], args.negative_size, replace=False, p=weight0)
        label_1 = np.random.choice(features_by_label[1], args.positive_size, replace=False, p=weight1)
    elif args.sampling_method == 'random' or (args.sampling_method == 'tardy' and ep <= 2):  # random sampling
        logger.info(" => Random sampling ...")
        label_0 = np.random.choice(features_by_label[0], args.negative_size, replace=False)
        label_1 = np.random.choice(features_by_label[1], args.positive_size, replace=False)
    total = np.concatenate((label_0, label_1))
    train_data, _ = get_tensor_dataset(args, total, "classification")
    train_sampler = RandomSampler(train_data)
    train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size)
    return train_dataloader


def get_gated_sampling_dataloader(device, ep, args, features_by_label, pre_loss=0):
    threshold = 0.5
    if ep == 1:
        label_0 = np.random.choice(features_by_label[0], args.positive_size, replace=False)
        logger.info(" gated-sampling result: th: %.2f, neg: %d, pos: %d" %
                    (threshold, len(label_0), len(features_by_label[1])))
        total = np.concatenate((label_0, features_by_label[1]))
    else:
        while True:
            label_0_hard, label_0_easy = [], []
            for x in features_by_label[0]:
                if x.preprob0 <= threshold:
                    label_0_hard.append(x)
                else:
                    label_0_easy.append(x)
            logits = torch.tensor([[x.preprob0, x.preprob1] for x in label_0_hard], dtype=torch.float).to(device)
            labels = torch.tensor([0] * len(label_0_hard), dtype=torch.long).to(device)
            # score = np.sum(-np.log(np.array([x.preprob0 for x in label_0_hard])))
            score = CrossEntropyLoss(reduction='sum')(logits, labels).item()
            if (score > pre_loss or abs(score - pre_loss) < pre_loss * 0.1) and len(label_0_hard) > args.positive_size:
                break
            else:
                threshold += 0.02
                if threshold >= 1:
                    break

        logger.info(" gated-sampling result: th: %.2f, neg: %d, pos: %d" %
                    (threshold, len(label_0_hard), len(features_by_label[1])))
        n_easy_sample = math.ceil(len(label_0_hard) * 0.1)
        if len(label_0_easy) > n_easy_sample:
            label_0_easy = np.random.choice(label_0_easy, math.ceil(len(label_0_hard) * 0.1))
            logger.info(" add noisy (easy) samples 10 percents of %d = %d" %
                        (len(label_0_hard), int(math.ceil(len(label_0_hard) * 0.1))))
            total = np.concatenate((label_0_hard, label_0_easy, features_by_label[1]))
            logger.info(" total sampling size (%d + %d + %d ) = %d" %
                        (len(label_0_hard), len(label_0_easy), len(features_by_label[1]), len(total)))
        else:
            logger.info(" No EASY samples!")
            total = np.concatenate((label_0_hard, features_by_label[1]))
            logger.info(" total sampling size (%d + %d) = %d" %
                        (len(label_0_hard), len(features_by_label[1]), len(total)))

    train_data, _ = get_tensor_dataset(args, total, "classification")
    train_sampler = RandomSampler(train_data)
    train_dataloader = DataLoader(train_data, sampler=train_sampler, batch_size=args.train_batch_size)
    return train_dataloader


def get_tensor_dataset(args, features, output_mode):
    if args.BERT:
        all_input_ids = torch.tensor([f.input_ids for f in features], dtype=torch.long)
        all_input_mask = torch.tensor([f.input_mask for f in features], dtype=torch.long)
        all_segment_ids = torch.tensor([f.segment_ids for f in features], dtype=torch.long)
        if output_mode == "classification":
            all_label_ids = torch.tensor([f.label_id for f in features], dtype=torch.long)
        elif output_mode == "regression":
            all_label_ids = torch.tensor([f.label_id for f in features], dtype=torch.float)
        all_preprob = torch.tensor([f.preprob for f in features], dtype=torch.float)
        train_data = TensorDataset(all_input_ids, all_input_mask, all_segment_ids, all_label_ids, all_preprob)
    elif args.task_name in ["cifar-10", "mnist", "svhn"]:
        all_inputs = torch.tensor([f.vector for f in features], dtype=torch.float)
        all_label_ids = torch.tensor([f.label_id for f in features], dtype=torch.long)
        train_data = TensorDataset(all_inputs, all_label_ids)
    else:
        all_input_ids_a = torch.tensor([f.input_ids_a for f in features], dtype=torch.long)
        all_input_ids_b = torch.tensor([f.input_ids_b for f in features], dtype=torch.long)
        all_label_ids = torch.tensor([f.label_id for f in features], dtype=torch.long)
        all_preprob = torch.tensor([f.preprob for f in features], dtype=torch.float)
        train_data = TensorDataset(all_input_ids_a, all_input_ids_b, all_label_ids, all_preprob)
    return train_data, all_label_ids


def KL_loss(p, q):
    var1 = p.var()
    mu1 = p.mean()
    var2 = q.var()
    mu2 = q.mean()
    # return 0.5 * (torch.log(var2 / var1) + (var1 / var2) - 1)
    return 0.5 * (torch.log(var2 / var1) + ((var1 + (mu1 - mu2) * (mu1 - mu2)) / var2) - 1)


if __name__ == "__main__":
    main()
