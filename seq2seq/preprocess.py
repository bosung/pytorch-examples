from const import *
import torch
import torch.autograd as autograd

class Vocab:

    def __init__(self):
        self.word2index = {}
        self.word2count = {}
        self.index2word = {0: "<SOS>", 1: "<EOS>"}
        self.n_words = 2

    def addSentence(self, sentence):
        for l in sentence.split('\t'):
            for word in l.split(' '):
                self.addWord(word)

    def addWord(self, word):
        if word not in self.word2index:
            self.word2index[word] = self.n_words
            self.word2count[word] = 1
            self.index2word[self.n_words] = word
            self.n_words += 1
        else:
            self.word2count[word] += 1

    def build(self, path):
        print("Building vocabulary from text...")

        lines = open(path, encoding='utf-8').read().strip().split('\n')
        for line in lines:
            self.addSentence(line)

        print("[INFO] total %s words" % self.n_words)


def read_train_data(path):
    #path = 'data/cqa_train.txt'
    lines = open(path, encoding='utf-8').read().strip().split('\n')
    pairs = [[s for s in l.split('\t')] for l in lines]
    print("[INFO] read train data: %s ..." % pairs[0])
    return pairs


def indexesFromSentence(vocab, sentence):
    return [vocab.word2index[word] for word in sentence.split(' ')]


def tensorFromSentence(vocab, sentence):
    indexes = indexesFromSentence(vocab, sentence)
    indexes.append(EOS_token)
    return torch.tensor(indexes, dtype=torch.long, device=device).view(-1, 1)


def tensorsFromPair(vocab, pair):
    input_tensor = tensorFromSentence(vocab, pair[0])
    target_tensor = tensorFromSentence(vocab, pair[1])
    return (input_tensor, target_tensor)

