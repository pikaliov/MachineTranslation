from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import math
import os
import random
import sys
import time
import logging

import numpy as np
from six.moves import xrange  # pylint: disable=redefined-builtin
import tensorflow as tf

import data_utils
import seq2seq_model
from nltk.tokenize import sent_tokenize


tf.app.flags.DEFINE_float("learning_rate", 0.5, "Learning rate.")
tf.app.flags.DEFINE_float("learning_rate_decay_factor", 0.99,
                          "Learning rate decays by this much.")
tf.app.flags.DEFINE_float("max_gradient_norm", 5.0,
                          "Clip gradients to this norm.")
tf.app.flags.DEFINE_integer("batch_size", 64,
                            "Batch size to use during training.")
tf.app.flags.DEFINE_integer("size", 1024, "Size of each model layer.")
tf.app.flags.DEFINE_integer("num_layers", 3, "Number of layers in the model.")
tf.app.flags.DEFINE_integer("from_vocab_size", 40000, "English vocabulary size.")
tf.app.flags.DEFINE_integer("to_vocab_size", 40000, "French vocabulary size.")
tf.app.flags.DEFINE_string("data_dir", "/tmp", "Data directory")
tf.app.flags.DEFINE_string("train_dir", "/tmp", "Training directory.")
tf.app.flags.DEFINE_string("from_train_data", None, "Training data.")
tf.app.flags.DEFINE_string("to_train_data", None, "Training data.")
tf.app.flags.DEFINE_string("from_dev_data", None, "Training data.")
tf.app.flags.DEFINE_string("to_dev_data", None, "Training data.")
tf.app.flags.DEFINE_integer("max_train_data_size", 0,
                            "Limit on the size of training data (0: no limit).")
tf.app.flags.DEFINE_integer("steps_per_checkpoint", 200,
                            "How many training steps to do per checkpoint.")
tf.app.flags.DEFINE_boolean("decode", False,
                            "Set to True for interactive decoding.")
tf.app.flags.DEFINE_boolean("self_test", False,
                            "Run a self-test if this is set to True.")
tf.app.flags.DEFINE_boolean("use_fp16", False,
                            "Train using fp16 instead of fp32.")
tf.app.flags.DEFINE_string("log_dir", "/Users/prateepm/Documents/summary_logs",
                           "Summary folder")

FLAGS = tf.app.flags.FLAGS

# We use a number of buckets and pad to the closest one for efficiency.
_buckets = [(5, 10), (10, 15), (20, 25), (40, 50)]


def read_data(source_path, target_path, max_size=None):
  data_set = [[] for _ in _buckets]
  with tf.gfile.GFile(source_path, mode="r") as source_file:
    with tf.gfile.GFile(target_path, mode="r") as target_file:
      source, target = source_file.readline(), target_file.readline()
      counter = 0
      while source and target and (not max_size or counter < max_size):
        counter += 1
        if counter % 100000 == 0:
          print("  reading data line %d" % counter)
          sys.stdout.flush()
        source_ids = [int(x) for x in source.split()]
        target_ids = [int(x) for x in target.split()]
        target_ids.append(data_utils.EOS_ID)
        for bucket_id, (source_size, target_size) in enumerate(_buckets):
          if len(source_ids) < source_size and len(target_ids) < target_size:
            data_set[bucket_id].append([source_ids, target_ids])
            break
        source, target = source_file.readline(), target_file.readline()
  return data_set


def create_model(session, forward_only):
  """Create translation model and initialize or load parameters in session."""
  dtype = tf.float16 if FLAGS.use_fp16 else tf.float32
  model = seq2seq_model.BidirModel(
      FLAGS.from_vocab_size,
      FLAGS.to_vocab_size,
      _buckets,
      FLAGS.size,
      FLAGS.num_layers,
      FLAGS.max_gradient_norm,
      FLAGS.batch_size,
      FLAGS.learning_rate,
      FLAGS.learning_rate_decay_factor,
      forward_only=forward_only,
      dtype=dtype)
  ckpt = tf.train.get_checkpoint_state(FLAGS.train_dir)
  if ckpt and tf.train.checkpoint_exists(ckpt.model_checkpoint_path):
    print("Reading model parameters from %s" % ckpt.model_checkpoint_path)
    model.saver.restore(session, ckpt.model_checkpoint_path)
  else:
    print("Created model with fresh parameters.")
    session.run(tf.global_variables_initializer())
  return model


def train():
  """Train a en->fr translation model using WMT data."""
  from_train = None
  to_train = None
  from_dev = None
  to_dev = None
  if FLAGS.from_train_data and FLAGS.to_train_data:
    from_train_data = FLAGS.from_train_data
    to_train_data = FLAGS.to_train_data
    from_dev_data = from_train_data
    to_dev_data = to_train_data
    if FLAGS.from_dev_data and FLAGS.to_dev_data:
      from_dev_data = FLAGS.from_dev_data
      to_dev_data = FLAGS.to_dev_data
    from_train, to_train, from_dev, to_dev, _, _ = data_utils.prepare_data(
        FLAGS.data_dir,
        from_train_data,
        to_train_data,
        from_dev_data,
        to_dev_data,
        FLAGS.from_vocab_size,
        FLAGS.to_vocab_size)
  else:
      # Prepare WMT data.
      print("Preparing WMT data in %s" % FLAGS.data_dir)
      from_train, to_train, from_dev, to_dev, _, _ = data_utils.prepare_wmt_data(
          FLAGS.data_dir, FLAGS.from_vocab_size, FLAGS.to_vocab_size)

  # Merge all summaries and write them out
  merged = tf.summary.merge_all()
  train_writer = tf.summary.FileWriter(FLAGS.log_dir + '/train')

  with tf.Session() as sess:
    # Create model.
    print("Creating %d layers of %d units." % (FLAGS.num_layers, FLAGS.size))
    model = create_model(sess, False)
    train_writer.add_graph(sess.graph)

    # Read data into buckets and compute their sizes.
    print ("Reading development and training data (limit: %d)."
           % FLAGS.max_train_data_size)
    dev_set = read_data(from_dev, to_dev)
    train_set = read_data(from_train, to_train, FLAGS.max_train_data_size)
    train_bucket_sizes = [len(train_set[b]) for b in xrange(len(_buckets))]
    train_total_size = float(sum(train_bucket_sizes))

    train_buckets_scale = [sum(train_bucket_sizes[:i + 1]) / train_total_size for i in xrange(len(train_bucket_sizes))]

    # This is the training loop.
    step_time, loss = 0.0, 0.0
    current_step = 0
    previous_losses = []
    while True:
      random_number_01 = np.random.random_sample()
      bucket_id = min([i for i in xrange(len(train_buckets_scale)) if train_buckets_scale[i] > random_number_01])

      start_time = time.time()
      encoder_inputs, decoder_inputs, target_weights = model.get_batch(
          train_set, bucket_id)
      _, step_loss, _ = model.step(sess, encoder_inputs, decoder_inputs,
                                   target_weights, bucket_id, False)
      step_time += (time.time() - start_time) / FLAGS.steps_per_checkpoint
      loss += step_loss / FLAGS.steps_per_checkpoint
      current_step += 1

      if current_step % FLAGS.steps_per_checkpoint == 0:
        perplexity = math.exp(float(loss)) if loss < 300 else float("inf")
        print ("global step %d learning rate %.4f step-time %.2f perplexity "
               "%.2f" % (model.global_step.eval(), model.learning_rate.eval(),
                         step_time, perplexity))
        # Decrease learning rate if no improvement was seen over last 3 times.
        if len(previous_losses) > 2 and loss > max(previous_losses[-3:]):
          sess.run(model.learning_rate_decay_op)
        previous_losses.append(loss)
        # Save checkpoint and zero timer and loss.
        checkpoint_path = os.path.join(FLAGS.train_dir, "translate.ckpt")
        model.saver.save(sess, checkpoint_path, global_step=model.global_step)
        step_time, loss = 0.0, 0.0
        # Run evals on development set and print their perplexity.
        for bucket_id in xrange(len(_buckets)):
          if len(dev_set[bucket_id]) == 0:
            print("  eval: empty bucket %d" % (bucket_id))
            continue
          encoder_inputs, decoder_inputs, target_weights = model.get_batch(
              dev_set, bucket_id)
          _, eval_loss, _ = model.step(sess, encoder_inputs, decoder_inputs,
                                       target_weights, bucket_id, True)
          eval_ppx = math.exp(float(eval_loss)) if eval_loss < 300 else float(
              "inf")
          print("  eval: bucket %d perplexity %.2f" % (bucket_id, eval_ppx))
        sys.stdout.flush()

  train_writer.close()


def decode():
  with tf.Session() as sess:
    model = create_model(sess, True)
    model.batch_size = 1  # We decode one sentence at a time.

    # Load vocabularies.
    en_vocab_path = os.path.join(FLAGS.data_dir,
                                 "vocab%d.from" % FLAGS.from_vocab_size)
    fr_vocab_path = os.path.join(FLAGS.data_dir,
                                 "vocab%d.to" % FLAGS.to_vocab_size)
    en_vocab, _ = data_utils.initialize_vocabulary(en_vocab_path)
    _, rev_fr_vocab = data_utils.initialize_vocabulary(fr_vocab_path)

    # Decode from standard input.
    sys.stdout.write("> ")
    sys.stdout.flush()
    sentence = sys.stdin.readline()
    while sentence:
      # Basic tokenize sentence to check for multiple sentences
      sentence_list = sent_tokenize(sentence, language='english')
      # print(len(sentence_list))
      # sys.stdout.flush()
      if len(sentence_list) > 1:
          # logging.warning("In multiple sentences")
          multiple_outputs = []
          for sent in sentence_list:
              # print("Sentence :%s" % sent)
              # Which bucket each sent belongs to?
              sent_token_ids = data_utils.sentence_to_token_ids(tf.compat.as_bytes(sent), en_vocab)
              # print(sent_token_ids)
              sent_bucket_id = len(_buckets) - 1
              for sent_i, sent_bucket in enumerate(_buckets):
                  if sent_bucket[0] >= len(sent_token_ids):
                      sent_bucket_id = sent_i
                      break
                  # else:
                      #logging.warning("Sentence truncated: %s" % sent)

              # print("Sent bucket id %d" % sent_bucket_id)
              # Get a 1-element batch to feed sent to the model
              sent_encoder_inputs, sent_decoder_inputs, sent_target_weights = model.get_batch(
                  {sent_bucket_id: [(sent_token_ids, [])]}, sent_bucket_id)

              # Get output logits for sent
              _, _, sent_output_logits = model.step(sess, sent_encoder_inputs, sent_decoder_inputs,
                                                    sent_target_weights, sent_bucket_id, True)
              # This is a greedy decoder - outputs are just argmaxes of output logits
              sent_outputs = [int(np.argmax(sent_logit, axis=1)) for sent_logit in sent_output_logits]
              # If there is an EOS symbol in sent_outputs, cut them at that point.
              if data_utils.EOS_ID in sent_outputs:
                  sent_outputs = sent_outputs[:sent_outputs.index(data_utils.EOS_ID)]
              # Finally append German sentence for sent_outputs to final output
              sent_translated = " ".join([tf.compat.as_str(rev_fr_vocab[sent_output]) for sent_output in sent_outputs])
              # print(sent_translated)
              multiple_outputs.append(sent_translated)

          print(" ".join(multiple_outputs))
          sys.stdout.flush()
      else:
          # print("In one, Sentence :%s" % sentence)
          # sys.stdout.flush()
          # Get token-ids for the input sentence.
          token_ids = data_utils.sentence_to_token_ids(tf.compat.as_bytes(sentence), en_vocab)
          # Which bucket does it belong to?
          bucket_id = len(_buckets) - 1
          for i, bucket in enumerate(_buckets):
            if bucket[0] >= len(token_ids):
              bucket_id = i
              break
            # else:
            #   logging.warning("Sentence truncated: %s", sentence)

          # Get a 1-element batch to feed the sentence to the model.
          encoder_inputs, decoder_inputs, target_weights = model.get_batch(
                  {bucket_id: [(token_ids, [])]}, bucket_id)
          # Get output logits for the sentence.
          _, _, output_logits = model.step(sess, encoder_inputs, decoder_inputs,
                                               target_weights, bucket_id, True)
          # This is a greedy decoder - outputs are just argmaxes of output_logits.
          outputs = [int(np.argmax(logit, axis=1)) for logit in output_logits]
          # If there is an EOS symbol in outputs, cut them at that point.
          if data_utils.EOS_ID in outputs:
             outputs = outputs[:outputs.index(data_utils.EOS_ID)]
          # Print out French sentence corresponding to outputs.
          print(" ".join([tf.compat.as_str(rev_fr_vocab[output]) for output in outputs]))

      print("> ", end="")
      sys.stdout.flush()
      sentence = sys.stdin.readline()


def self_test():
  with tf.Session() as sess:
    print("Self-test for neural translation model.")
    # Create model with vocabularies of 10, 2 small buckets, 2 layers of 32.
    model = seq2seq_model.BidirModel(10, 10, [(3, 3), (6, 6)], 32, 2,
                                       5.0, 32, 0.3, 0.99, num_samples=8)
    sess.run(tf.global_variables_initializer())

    # Fake data set for both the (3, 3) and (6, 6) bucket.
    data_set = ([([1, 1], [2, 2]), ([3, 3], [4]), ([5], [6])],
                [([1, 1, 1, 1, 1], [2, 2, 2, 2, 2]), ([3, 3, 3], [5, 6])])
    for _ in xrange(5):  # Train the fake model for 5 steps.
      bucket_id = random.choice([0, 1])
      encoder_inputs, decoder_inputs, target_weights = model.get_batch(
          data_set, bucket_id)
      model.step(sess, encoder_inputs, decoder_inputs, target_weights,
                 bucket_id, False)


def main(_):
  if FLAGS.self_test:
    self_test()
  elif FLAGS.decode:
    decode()
  else:
    train()

if __name__ == "__main__":
  tf.app.run()