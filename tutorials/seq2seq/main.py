from model import Encoder, Decoder, AttentionDecoder
from preprocess import Vocab
import preprocess as prep
import evaluate as ev

import argparse
import time
import torch
import torch.nn as nn
import torch.utils.data as data
from torch import optim
import random

from utils import *
from const import *

global batch_size


def train(input_tensor, target_tensor, encoder, decoder, encoder_optimizer, decoder_optimizer, criterion, max_length=MAX_LENGTH):

    encoder_optimizer.zero_grad()
    decoder_optimizer.zero_grad()

    encoder_hidden = encoder.init_hidden(batch_size)
    encoder_outputs = torch.zeros(max_length, batch_size, encoder.hidden_size, device=device)

    loss = 0

    input_tensor = input_tensor.transpose(0, 1)

    for ei in range(max_length):
        it = input_tensor[ei].view(batch_size, -1)
        encoder_output, encoder_hidden = encoder(it, encoder_hidden)
        encoder_outputs[ei] = encoder_output.transpose(1, 2).view(batch_size, encoder.hidden_size)

    decoder_input = torch.tensor([batch_size * [SOS_token]], device=device).view(batch_size, 1)
    decoder_hidden = encoder_hidden

    target_tensor = target_tensor.transpose(0, 1)

    for di in range(max_length):
        #decoder_output, decoder_hidden, decoder_attention = decoder(
        decoder_output, decoder_hidden = decoder(decoder_input, decoder_hidden)
        #    decoder_input, decoder_hidden, encoder_outputs)
        loss += criterion(decoder_output, target_tensor[di])
        decoder_input = target_tensor[di]  # Teacher forcing

    loss.backward(retain_graph=True)

    encoder_optimizer.step()
    decoder_optimizer.step()

    return loss.item() / max_length


def trainIters(args, epoch, encoder, decoder, n_iters, pairs, vocab, train_loader,
        print_every=1000, plot_every=100, learning_rate=0.01):
    start = time.time()
    plot_losses = []
    print_loss_total = 0  # Reset every print_every
    plot_loss_total = 0  # Reset every plot_every

    if args.optim == 'RMSprop':
        encoder_optimizer = optim.RMSprop(encoder.parameters(), lr=learning_rate)
        decoder_optimizer = optim.RMSprop(decoder.parameters(), lr=learning_rate)
    elif args.optim == 'Adam':
        encoder_optimizer = optim.Adam(encoder.parameters(), lr=learning_rate)
        decoder_optimizer = optim.Adam(decoder.parameters(), lr=learning_rate)
    elif args.optim == 'SGD':
        encoder_optimizer = optim.SGD(encoder.parameters(), lr=learning_rate)
        decoder_optimizer = optim.SGD(decoder.parameters(), lr=learning_rate)

    criterion = nn.NLLLoss()

    num_iters = 0
    for _iter, (batch_input, batch_target) in enumerate(train_loader):
        input_tensor = prep.tensorFromSentenceBatchWithPadding(vocab, batch_input)
        target_tensor = prep.tensorFromSentenceBatchWithPadding(vocab, batch_target)
        loss = train(input_tensor, target_tensor, encoder,
                     decoder, encoder_optimizer, decoder_optimizer, criterion)
        print_loss_total += loss
        plot_loss_total += loss
        num_iters += batch_size

    if epoch % print_every == 0:
        print_loss_avg = print_loss_total / num_iters
        print_loss_total = 0
        print('%s (%d %d%%) %.4f' % (timeSince(start, epoch / n_iters),
                                epoch, epoch / n_iters * 100, print_loss_avg))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument('--encoder', help='load exisited model')
    parser.add_argument('--decoder', help='load exisited model')
    parser.add_argument('--optim', default='RMSprop')
    parser.add_argument('--batch_size', type=int, default=40)
    parser.add_argument('--hidden_size', type=int, default=128)
    parser.add_argument('--w_embed_size', type=int, default=64)
    parser.add_argument('--lr', type=float, default=0.001)
    parser.add_argument('--epoch', type=int, default=400)
    parser.add_argument('--save', choices=['y', 'n'], default='n')
    parser.add_argument('--pre_trained_embed', choices=['y', 'n'], default='y')
    args = parser.parse_args()

    global batch_size
    batch_size = args.batch_size
    hidden_size = args.hidden_size
    w_embed_size = args.w_embed_size

    train_file = 'data/train_data_nv.txt'

    vocab = Vocab()
    vocab.build(train_file)

    if args.pre_trained_embed == 'n':
        encoder = Encoder(vocab.n_words, w_embed_size, hidden_size, batch_size).to(device)
        decoder = AttentionDecoder(vocab.n_words, w_embed_size, hidden_size, batch_size).to(device)
        # decoder = Decoder(vocab.n_words, w_embed_size, hidden_size, batch_size).to(device)
    else:
        # load pre-trained embedding
        weight = vocab.load_weight(path="data/komoran_hd_2times.vec")
        encoder = Encoder(vocab.n_words, w_embed_size, hidden_size, batch_size, weight).to(device)
        decoder = AttentionDecoder(vocab.n_words, w_embed_size, hidden_size, batch_size, weight).to(device)
        # decoder = Decoder(vocab.n_words, w_embed_size, hidden_size, batch_size, weight).to(device)

    if args.encoder:
        encoder.load_state_dict(torch.load(args.encoder))
        print("[INFO] load encoder with %s" % args.encoder)
    if args.decoder:
        decoder.load_state_dict(torch.load(args.decoder))
        print("[INFO] load decoder with %s" % args.decoder)

    train_data = prep.read_train_data(train_file)
    train_loader = data.DataLoader(train_data, batch_size=batch_size, shuffle=True)

    # ev.evaluateRandomly(encoder, decoder, train_data, vocab, batch_size)
    # ev.evaluate_with_print(encoder, vocab, batch_size)

    # initialize
    max_a_at_5, max_a_at_1 = ev.evaluate_similarity(encoder, vocab, batch_size, decoder=decoder)
    # max_a_at_5, max_a_at_1 = 0, 0
    max_bleu = 0

    total_epoch = args.epoch
    print(args)
    for epoch in range(1, total_epoch+1):
        random.shuffle(train_data)
        trainIters(args, epoch, encoder, decoder, total_epoch, train_data, vocab, train_loader, print_every=2, learning_rate=0.001)

        if epoch % 20 == 0:
            a_at_5, a_at_1 = ev.evaluate_similarity(encoder, vocab, batch_size, decoder=decoder)

            if a_at_1 > max_a_at_1:
                max_a_at_1 = a_at_1
                print("[INFO] New record! accuracy@1: %.4f" % a_at_1)
                # if args.save == 'y':
                #    torch.save(encoder.state_dict(), 'encoder-max.model')
                #    torch.save(decoder.state_dict(), 'decoder-max.model')
                #    print("[INFO] new model saved")

            if a_at_5 > max_a_at_5:
                max_a_at_5 = a_at_5
                print("[INFO] New record! accuracy@5: %.4f" % a_at_5)

            bleu = ev.evaluateRandomly(encoder, decoder, train_data, vocab, batch_size)
            if bleu > max_bleu:
                max_bleu = bleu
                if args.save == 'y':
                    torch.save(encoder.state_dict(), 'encoder-max.model')
                    torch.save(decoder.state_dict(), 'decoder-max.model')
                    print("[INFO] new model saved")

    print("Done! max accuracy@5: %.4f, max accuracy@1: %.4f" % (max_a_at_5, max_a_at_1))
    print("max bleu: %.2f" % max_bleu)
    if args.save == 'y':
        torch.save(encoder.state_dict(), 'encoder-last.model')
        torch.save(decoder.state_dict(), 'decoder-last.model')

