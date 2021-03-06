import numpy as np
import os
import random
import re
import sys
import time
from keras import models, layers
from PIL import Image
from StringIO import StringIO

from keras.applications import resnet50
import tensorflow as tf
import words
import dataset_grefexp
import bleu_scorer
import rouge_scorer
import util
from util import MAX_WORDS

IMG_WIDTH = 224
IMG_HEIGHT = 224
IMG_CHANNELS = 3
IMG_SHAPE = (IMG_HEIGHT, IMG_WIDTH, IMG_CHANNELS)

# Learn the softmax layer and the conv/batchnorm behind it
LEARNABLE_RESNET_LAYERS = 7

def build_model(GRU_SIZE=1024, WORDVEC_SIZE=300, ACTIVATION='relu'):
    resnet = build_resnet()

    # Global Image featuers (convnet output for the whole image)
    input_img_global = layers.Input(shape=IMG_SHAPE)
    image_global = resnet(input_img_global)
    image_global = layers.BatchNormalization()(image_global)
    image_global = layers.Dense(WORDVEC_SIZE/2, activation=ACTIVATION)(image_global)
    image_global = layers.BatchNormalization()(image_global)
    image_global = layers.RepeatVector(MAX_WORDS)(image_global)


    # Local Image features (convnet output inside the bounding box)
    input_img_local = layers.Input(shape=IMG_SHAPE)
    image_local = resnet(input_img_local)
    image_local = layers.BatchNormalization()(image_local)
    image_local = layers.Dense(WORDVEC_SIZE/2, activation=ACTIVATION)(image_local)
    image_local = layers.BatchNormalization()(image_local)
    image_local = layers.RepeatVector(MAX_WORDS)(image_local)


    # Context Vector input
    # normalized to [0,1] the values:
    # left, top, right, bottom, (box area / image area)
    input_ctx = layers.Input(shape=(5,))
    ctx = layers.BatchNormalization()(input_ctx)
    ctx = layers.RepeatVector(MAX_WORDS)(ctx)

    language_model = models.Sequential()

    input_words = layers.Input(shape=(MAX_WORDS,), dtype='int32')
    language = layers.Embedding(words.VOCABULARY_SIZE, WORDVEC_SIZE, input_length=MAX_WORDS)(input_words)
    language = layers.BatchNormalization()(language)
    language = layers.GRU(GRU_SIZE, return_sequences=True)(language)
    language = layers.BatchNormalization()(language)
    language = layers.TimeDistributed(layers.Dense(WORDVEC_SIZE, activation=ACTIVATION))(language)
    language = layers.BatchNormalization()(language)

    # Problem with Keras 2: 
    # TypeError: Tensors in list passed to 'values' of 'ConcatV2' Op have types [uint8, uint8, bool, uint8] that don't all match.
    # Masking doesn't work along with concatenation.
    # How do I get mask_zero=True working in the embed layer?

    x = layers.concatenate([image_global, image_local, ctx, language])
    x = layers.GRU(GRU_SIZE)(x)
    x = layers.BatchNormalization()(x)
    x = layers.Dense(words.VOCABULARY_SIZE, activation='softmax')(x)

    return models.Model(inputs=[input_img_global, input_img_local, input_words, input_ctx], outputs=x)


def build_resnet():
    resnet = resnet50.ResNet50(include_top=True)
    for layer in resnet.layers[:-LEARNABLE_RESNET_LAYERS]:
        layer.trainable = False
    return resnet


# TODO: Move batching out to the generic runner
def training_generator():
    while True:
        BATCH_SIZE = 32
        X_global = np.zeros((BATCH_SIZE, IMG_HEIGHT, IMG_WIDTH, IMG_CHANNELS))
        X_local = np.zeros((BATCH_SIZE, IMG_HEIGHT, IMG_WIDTH, IMG_CHANNELS))
        X_words = np.zeros((BATCH_SIZE, MAX_WORDS), dtype=int)
        X_ctx = np.zeros((BATCH_SIZE,5))
        Y = np.zeros((BATCH_SIZE, words.VOCABULARY_SIZE))
        for i in range(BATCH_SIZE):
            x, y = process(*dataset_grefexp.example())
            x_global, x_local, x_words, x_ctx = x
            X_global[i] = x_global
            X_local[i] = x_local
            X_words[i] = x_words
            X_ctx[i] = x_ctx
            Y[i] = y
        yield [X_global, X_local, X_words, X_ctx], Y


def process(jpg_data, box, texts):
    x_local = util.decode_jpg(jpg_data, crop_to_box=box)
    # hack: scale the box down
    x_global, box = util.decode_jpg(jpg_data, box)
    text = util.strip(random.choice(texts))
    indices = words.indices(text)
    idx = np.random.randint(0, len(indices))
    x_words = util.left_pad(indices[:idx][-MAX_WORDS:])
    y = util.onehot(indices[idx])
    x_ctx = img_ctx(box)
    return [x_global, x_local, x_words, x_ctx], y


def img_ctx(box):
    x0, x1, y0, y1 = box
    left = float(x0) / IMG_WIDTH
    right = float(x1) / IMG_WIDTH
    top = float(y0) / IMG_HEIGHT
    bottom = float(y1) / IMG_HEIGHT
    box_area = float(x1 - x0) * (y1 - y0)
    img_area = IMG_HEIGHT * IMG_WIDTH
    x_ctx = np.array([left, top, right, bottom, box_area/img_area])
    return x_ctx


def validation_generator():
    for k in dataset_grefexp.get_all_keys():
        jpg_data, box, texts = dataset_grefexp.get_annotation_for_key(k)
        x, y = process(jpg_data, box, texts)
        x_global, x_local, x_words, x_ctx = x
        yield x_global, x_local, x_ctx, box, texts


def evaluate(model, x_global, x_local, x_ctx, box, texts, temperature=.0):
    candidate, likelihood = predict(model, x_global, x_local, x_ctx, box, temperature)
    candidate = util.strip(candidate)
    references = map(util.strip, texts)
    print("{} {} ({})".format(likelihood, candidate, references[0]))
    scores = {}
    scores['bleu1'], scores['bleu2'] = bleu(candidate, references)
    scores['rouge'] = rouge(candidate, references)
    scores['likelihood'] = likelihood
    return scores


def predict(model, x_global, x_local, x_ctx, box, temperature=.0):
    indices = util.left_pad([])
    #x0, x1, y0, y1 = box
    #coords = [0, (y0 + y1) / 2, (x0 + x1) / 2]
    likelihoods = []
    for i in range(MAX_WORDS):
        preds = model.predict([util.expand(x_global), util.expand(x_local), util.expand(indices), util.expand(x_ctx)])
        preds = preds[0]
        indices = np.roll(indices, -1)
        if temperature > 0:
            indices[-1] = sample(preds, temperature)
        else:
            indices[-1] = np.argmax(preds, axis=-1)
        likelihoods.append(preds[indices[-1]])
    return words.words(indices), np.mean(likelihoods)


def sample(preds, temperature=1.0):
    # helper function to sample an index from a probability array
    preds = np.asarray(preds).astype('float64')
    preds = np.log(preds) / temperature
    exp_preds = np.exp(preds)
    preds = exp_preds / np.sum(exp_preds)
    probas = np.random.multinomial(1, preds, 1)
    return np.argmax(probas)


def bleu(candidate, references):
    scores, _ = bleu_scorer.BleuScorer(candidate, references, n=2).compute_score(option='closest')
    return scores


def rouge(candidate, references):
    return rouge_scorer.Rouge().calc_score([candidate], references)


def demo(model):
    for f in ['cat.jpg', 'dog.jpg', 'horse.jpg', 'car.jpg']:
        x_global = util.decode_jpg(f)
        height, width, _ = x_global.shape
        box = (width * .25, width * .75, height * .25, height * .75)
        x_local = util.decode_jpg(f, crop_to_box=box)
        x_ctx = img_ctx(box)
        print("Prediction for {} {}:".format(f, box)),
        print(predict(model, x_global, x_local, x_ctx, box))
