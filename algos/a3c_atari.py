#!/usr/bin/env python
# Quick-n-dirty implementation of Advantage Actor-Critic method from https://arxiv.org/abs/1602.01783
import os
import argparse
import logging
import numpy as np

logger = logging.getLogger()
logger.setLevel(logging.INFO)

from keras.optimizers import Adam
from keras import backend as K
import tensorflow as tf

from algo_lib.common import make_env
from algo_lib.atari_opts import HISTORY_STEPS, net_input
from algo_lib.a3c import make_run_model, make_train_model


PLAYERS_COUNT = 50

BATCH_SIZE = 128
SYNC_MODEL_EVERY_BATCH = 1
SAVE_MODEL_EVERY_BATCH = 3000


class Player:
    def __init__(self, env, model, reward_steps, gamma, max_steps, player_index):
        self.env = env
        self.model = model
        self.reward_steps = reward_steps
        self.gamma = gamma
        self.state = env.reset()

        self.memory = []
        self.episode_reward = 0.0
        self.step_index = 0
        self.max_steps = max_steps
        self.player_index = player_index

    def play(self, steps):
        result = []

        for _ in range(steps):
            self.step_index += 1
            probs, value = self.model.predict_on_batch([
                np.array([self.state]),
            ])
            probs, value = probs[0], value[0][0]
            # take action
            action = np.random.choice(len(probs), p=probs)
            new_state, reward, done, _ = self.env.step(action)

            self.episode_reward += reward
            self.memory.append((self.state, action, reward, value))

            if done or self.step_index > self.max_steps:
                self.state = self.env.reset()
                logging.info("%3d: Episode done @ step %d: sum reward %d",
                             self.player_index, self.step_index, int(self.episode_reward))
                self.episode_reward = 0.0
                self.step_index = 0
                result.extend(self._memory_to_samples(is_done=done))
                break
            elif len(self.memory) == self.reward_steps + 1:
                result.extend(self._memory_to_samples(is_done=False))

            self.state = new_state

        return result

    def _memory_to_samples(self, is_done):
        """
        From existing memory, generate samples
        :param is_done: is episode done
        :return: list of training samples
        """
        result = []
        R, last_item = 0.0, None

        if not is_done:
            last_item = self.memory.pop()
            R = last_item[-1]

        for state, action, reward, value in reversed(self.memory):
            R = reward + R * self.gamma
            advantage = R - value
            result.append((state, action, R, advantage))

        self.memory = [] if is_done else [last_item]
        return result


def generate_batches(players, batch_size):
    samples = []

    while True:
        for player in players:
            samples.extend(player.play(1))
        while len(samples) >= batch_size:
            states, actions, rewards, advantages = list(map(np.array, zip(*samples[:batch_size])))
            yield [states, actions, advantages], [rewards, rewards]
            samples = samples[batch_size:]


def make_value_summary(name, value):
    summ = tf.Summary()
    summ_value = summ.value.add()
    summ_value.simple_value = value
    summ_value.tag = name
    return summ


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", "--name", required=True, help="Run name")
    parser.add_argument("-e", "--env", default="Breakout-v0", help="Environment name to use")
    parser.add_argument("-m", "--monitor", help="Enable monitor and save data into provided dir, default=disabled")
    parser.add_argument("--gamma", type=float, default=0.99, help="Gamma for reward discount, default=0.99")
    parser.add_argument("-i", "--iters", type=int, default=10000, help="Count of iterations to take, default=100")
    parser.add_argument("--steps", type=int, default=5, help="Count of steps to use in reward estimation")
    args = parser.parse_args()

    # limit GPU memory
    config = tf.ConfigProto()
    config.gpu_options.per_process_gpu_memory_fraction = 0.2
    K.set_session(tf.Session(config=config))

    env = make_env(args.env, args.monitor, history_steps=HISTORY_STEPS)
    state_shape = env.observation_space.shape
    n_actions = env.action_space.n
    logger.info("Created environment %s, state: %s, actions: %s", args.env, state_shape, n_actions)

    tr_input_t, tr_conv_out_t = net_input(state_shape)
    value_policy_model = make_train_model(tr_input_t, tr_conv_out_t, n_actions)

    r_input_t, r_conv_out_t = net_input(state_shape)
    run_model = make_run_model(r_input_t, r_conv_out_t, n_actions)

    value_policy_model.summary()

    loss_dict = {
        'value': 'mse',
        'policy_loss': lambda y_true, y_pred: y_pred
    }

    value_policy_model.compile(optimizer=Adam(lr=0.001, epsilon=1e-3, clipnorm=0.1), loss=loss_dict)

    summary_writer = tf.summary.FileWriter("logs/" + args.name)

    players = [Player(make_env(args.env, args.monitor, history_steps=HISTORY_STEPS), run_model,
                      reward_steps=args.steps, gamma=args.gamma, max_steps=40000, player_index=idx)
               for idx in range(PLAYERS_COUNT)]

    # add gradient summaries
    gradients = value_policy_model.optimizer.get_gradients(value_policy_model.total_loss, value_policy_model._collected_trainable_weights)
    for var, grad in zip(value_policy_model._collected_trainable_weights, gradients):
        n = var.name.split(':', maxsplit=1)[0]
        tf.summary.scalar("gradrms_" + n, K.sqrt(K.mean(K.square(grad))))

    # add special metric
    value_policy_model.metrics_names.append("value_summary")
    value_policy_model.metrics_tensors.append(tf.summary.merge_all())

    for iter_idx, (x_batch, y_batch) in enumerate(generate_batches(players, BATCH_SIZE)):
        l = value_policy_model.train_on_batch(x_batch, y_batch)
        l_dict = dict(zip(value_policy_model.metrics_names, l))

        # write every other batch
        if iter_idx % 2 == 0:
            summary_writer.add_summary(make_value_summary("reward", np.mean(y_batch[0])), global_step=iter_idx)
            summary_writer.add_summary(make_value_summary("loss_value", l_dict['value_loss']), global_step=iter_idx)
            summary_writer.add_summary(make_value_summary("loss", l_dict['loss']), global_step=iter_idx)
            summary_writer.add_summary(l_dict['value_summary'], global_step=iter_idx)
            summary_writer.flush()
        if iter_idx % SYNC_MODEL_EVERY_BATCH == 0:
            run_model.set_weights(value_policy_model.get_weights())
#            logger.info("Models synchronized, iter %d", iter_idx)
        if iter_idx % SAVE_MODEL_EVERY_BATCH == 0 and iter_idx > 0:
            value_policy_model.save(os.path.join("logs", args.name, "model-%06d.h5" % iter_idx))
