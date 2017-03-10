import numpy as np
import os
import random
import re
import sys
import time
from keras import models, layers
from PIL import Image
from StringIO import StringIO

import resnet50
import tensorflow as tf
import words
import dataset_grefexp
import bleu_scorer
import rouge_scorer
import util
from util import MAX_WORDS


def build_model(GRU_SIZE=1024, WORDVEC_SIZE=300, ACTIVATION='relu'):
    resnet = build_resnet()

    input_img_global = layers.Input(shape=(224,224,3))
    image_global = resnet(input_img_global)
    image_global = layers.BatchNormalization()(image_global)
    image_global = layers.Dense(WORDVEC_SIZE/2, activation=ACTIVATION)(image_global)
    image_global = layers.BatchNormalization()(image_global)
    image_global = layers.RepeatVector(MAX_WORDS)(image_global)

    model_global = models.Model(input=input_img_global, output=image_global)

    input_img_local = layers.Input(shape=(224,224,3))
    image_local = resnet(input_img_local)
    image_local = layers.BatchNormalization()(image_local)
    image_local = layers.Dense(WORDVEC_SIZE/2, activation=ACTIVATION)(image_local)
    image_local = layers.BatchNormalization()(image_local)
    image_local = layers.RepeatVector(MAX_WORDS)(image_local)

    model_local = models.Model(input=input_img_local, output=image_local)

    # normalized to [0,1] the values:
    # left, top, right, bottom, (box area / image area)
    input_context_vector = layers.Input(shape=(5,))
    ctx = layers.BatchNormalization()(input_context_vector)
    ctx = layers.RepeatVector(MAX_WORDS)(ctx)
    context_model = models.Model(input=input_context_vector, output=ctx)

    language_model = models.Sequential()
    language_model.add(layers.Embedding(words.VOCABULARY_SIZE, WORDVEC_SIZE, input_length=MAX_WORDS, mask_zero=True))
    language_model.add(layers.BatchNormalization())
    language_model.add(layers.GRU(GRU_SIZE, return_sequences=True))
    language_model.add(layers.BatchNormalization())
    language_model.add(layers.TimeDistributed(layers.Dense(WORDVEC_SIZE, activation=ACTIVATION)))
    language_model.add(layers.BatchNormalization())
    
    model = models.Sequential()
    model.add(layers.Merge([model_global, model_local, language_model, context_model], mode='concat', concat_axis=-1))
    model.add(layers.GRU(GRU_SIZE, return_sequences=False))
    model.add(layers.BatchNormalization())
    model.add(layers.Dense(words.VOCABULARY_SIZE, activation='softmax'))

    return model

def build_resnet():
    resnet = resnet50.ResNet50(include_top=True)
    for layer in resnet.layers[:-1]:
        layer.trainable = False
    return resnet

# TODO: Move batching out to the generic runner
def training_generator():
    while True:
        BATCH_SIZE = 32
        X_global = np.zeros((BATCH_SIZE,224,224,3))
        X_local = np.zeros((BATCH_SIZE,224,224,3))
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


# TODO: don't assume image is 224x224
def img_ctx(box):
    x0, x1, y0, y1 = box
    left = x0 / 224.
    right = x1 / 224.
    top = y0 / 224.
    bottom = y1 / 224.
    box_area = float(x1 - x0) * (y1 - y0)
    img_area = 224 * 224.
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
    scores = []
    for i in range(MAX_WORDS):
        preds = model.predict([util.expand(x_global), util.expand(x_local), util.expand(indices), util.expand(x_ctx)])
        scores.append(preds.max())
        indices = np.roll(indices, -1)
        indices[-1] = sample(preds[0], temperature)
    return words.words(indices), np.mean(scores)


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
