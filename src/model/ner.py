import os
import json
from typing import Dict, List
from abc import abstractmethod
from itertools import chain

import tensorflow as tf
import numpy as np

from src.data.base import Example, Entity
from src.data.postprocessing import get_valid_spans
from src.model.base import BaseModel, BaseModelBert, ModeKeys
from src.model.layers import StackedBiRNN, GraphEncoder, GraphEncoderInputs
from src.model.utils import get_dense_labels_from_indices, upper_triangular
from src.metrics import classification_report, classification_report_ner
from src.utils import get_entity_spans, batches_gen


__all__ = ["BertForFlatNER", "BertForNestedNER"]


class BaseModelNER(BaseModel):
    ner_scope = "ner"

    def __init__(self, sess, config: Dict = None, ner_enc: Dict = None):
        super().__init__(sess=sess, config=config)
        self._ner_enc = None
        self._inv_ner_enc = None

        self.ner_enc = ner_enc

    def _build_graph(self):
        self._build_embedder()
        with tf.variable_scope(self.ner_scope):
            self._build_ner_head()

    def save(self, model_dir: str, force: bool = True, scope_to_save: str = None):
        super().save(model_dir=model_dir, force=force, scope_to_save=scope_to_save)

        with open(os.path.join(model_dir, "ner_enc.json"), "w") as f:
            json.dump(self.ner_enc, f, indent=4)

    @classmethod
    def load(cls, sess: tf.Session, model_dir: str, scope_to_load: str = None):
        model = super().load(sess=sess, model_dir=model_dir, scope_to_load=scope_to_load)

        with open(os.path.join(model_dir, "ner_enc.json")) as f:
            model.ner_enc = json.load(f)

        return model

    @abstractmethod
    def _build_ner_head(self):
        pass

    @property
    def ner_enc(self):
        return self._ner_enc

    @property
    def inv_ner_enc(self):
        return self._inv_ner_enc

    @ner_enc.setter
    def ner_enc(self, ner_enc: Dict):
        self._ner_enc = ner_enc
        if ner_enc is not None:
            self._inv_ner_enc = {v: k for k, v in ner_enc.items()}


class BertForFlatNER(BaseModelNER, BaseModelBert):
    """
    bert -> [bilstm x N] -> logits -> [crf]
    """

    def __init__(self, sess=None, config=None, ner_enc=None):
        """
        config = {
            "model": {
                "bert": {
                    "dir": "~/bert",
                    "dim": 768,
                    "attention_probs_dropout_prob": 0.5,  # default 0.1
                    "hidden_dropout_prob": 0.1,
                    "dropout": 0.1,
                    "scope": "bert",
                    "pad_token_id": 0,
                    "cls_token_id": 1,
                    "sep_token_id": 2
                },
                "ner": {
                    "use_crf": True,
                    "num_labels": 7,
                    "no_entity_id": 0,
                    "start_ids": [1, 2, 3],  # id лейблов первых токенов сущностей. нужно для векторизации сущностей
                    "prefix_joiner": "-",
                    "loss_coef": 1.0,
                    "use_birnn": True,
                    "rnn": {
                        "num_layers": 1,
                        "cell_dim": 128,
                        "dropout": 0.5,
                        "recurrent_dropout": 0.0
                    }
                },
            },
            "training": {
                "num_epochs": 100,
                "batch_size": 16,
                "max_epochs_wo_improvement": 10
            },
            "inference": {
                "window": 1,
                "max_tokens_per_batch: 10000
            },
            "optimizer": {
                "init_lr": 2e-5,
                "num_train_steps": 100000,
                "num_warmup_steps": 10000
            }
        }
        """
        super().__init__(sess=sess, config=config, ner_enc=ner_enc)

        # TENSORS
        self.ner_logits_train = None
        self.transition_params = None
        self.ner_preds_inference = None
        self.per_example_loss = None

        # LAYERS
        self.birnn_ner = None
        self.dense_ner_labels = None

    def _build_ner_head(self):
        self.bert_dropout = tf.keras.layers.Dropout(self.config["model"]["bert"]["dropout"])

        if self.config["model"]["ner"]["use_birnn"]:
            self.birnn_ner = StackedBiRNN(**self.config["model"]["ner"]["rnn"])

        self.dense_ner_labels = tf.keras.layers.Dense(self.config["model"]["ner"]["num_labels"])

        self.ner_logits_train, _, self.transition_params = self._build_ner_head_fn(bert_out=self.bert_out_train)
        _, self.ner_preds_inference, _ = self._build_ner_head_fn(bert_out=self.bert_out_pred)

    # TODO: профилирование!!!
    def evaluate(self, examples: List[Example], **kwargs) -> Dict:
        chunks = []
        for x in examples:
            assert len(x.chunks) > 0
            chunks += x.chunks

        y_true_ner = []
        y_pred_ner = []
        loss = []

        gen = batches_gen(
            examples=chunks,
            max_tokens_per_batch=self.config["inference"]["max_tokens_per_batch"],
            pieces_level=True
        )
        for batch in gen:
            feed_dict = self._get_feed_dict(batch, mode=ModeKeys.VALID)
            loss_i, ner_labels_pred = self.sess.run([self.per_example_loss, self.ner_preds_inference], feed_dict=feed_dict)
            loss.append(loss_i.flatten())

            for i, x in enumerate(batch):
                y_true_ner_i = []
                y_pred_ner_i = []
                for j, t in enumerate(x.tokens):
                    y_true_ner_i.append(t.labels[0])
                    y_pred_ner_i.append(self.inv_ner_enc[ner_labels_pred[i, j]])
                y_true_ner.append(y_true_ner_i)
                y_pred_ner.append(y_pred_ner_i)

        # loss
        loss = np.concatenate(loss).mean()

        # ner
        joiner = self.config["model"]["ner"]["prefix_joiner"]
        ner_metrics_entity_level = classification_report_ner(y_true=y_true_ner, y_pred=y_pred_ner, joiner=joiner)
        y_true_ner_flat = list(chain(*y_true_ner))
        y_pred_ner_flat = list(chain(*y_pred_ner))
        ner_metrics_token_level = classification_report(
            y_true=y_true_ner_flat, y_pred=y_pred_ner_flat, trivial_label="O"
        )

        score = ner_metrics_entity_level["micro"]["f1"]
        performance_info = {
            "loss": loss,
            "score": score,
            "metrics": {
                "entity_level": ner_metrics_entity_level,
                "token_level": ner_metrics_token_level
            }
        }

        return performance_info

    # TODO: реалзиовать случай window > 1
    def predict(self, examples: List[Example], **kwargs) -> None:
        """
        инференс. примеры не должны содержать разметку токенов и пар сущностей!
        сделано так для того, чтобы не было непредсказуемых результатов.

        ner - запись лейблов в Token.labels
        re - создание новых инстансов Arc и запись их в Example.arcs
        """
        # проверка примеров
        chunks = []
        for x in examples:
            assert len(x.chunks) > 0, f"[{x.id}] didn't split by chunks"
            for t in x.tokens:
                assert len(t.labels) == 0, f"[{x.id}] tokens are already annotated"
            for chunk in x.chunks:
                assert chunk.parent is not None, f"[{x.id}] parent for chunk {chunk.id} is not set. " \
                    f"It is not a problem, but must be set for clarity"
                chunks.append(chunk)

        id2example = {x.id: x for x in examples}
        assert len(id2example) == len(examples), f"examples must have unique ids, " \
            f"but got {len(id2example)} unique ids among {len(examples)} examples"

        gen = batches_gen(
            examples=chunks,
            max_tokens_per_batch=self.config["inference"]["max_tokens_per_batch"],
            pieces_level=True
        )
        for batch in gen:
            feed_dict = self._get_feed_dict(batch, mode=ModeKeys.TEST)
            ner_labels_pred = self.sess.run(self.ner_preds_inference, feed_dict=feed_dict)

            m = max(len(x.tokens) for x in batch)
            assert m == ner_labels_pred.shape[1], f'{m} != {ner_labels_pred.shape[1]}'

            for i, chunk in enumerate(batch):
                example = id2example[chunk.parent]
                ner_labels_i = []
                for j, t in enumerate(chunk.tokens):
                    id_label = ner_labels_pred[i, j]
                    label = self.inv_ner_enc[id_label]
                    ner_labels_i.append(label)

                tag2spans = get_entity_spans(labels=ner_labels_i, joiner=self.config["model"]["ner"]["prefix_joiner"])
                for label, spans in tag2spans.items():
                    for span in spans:
                        start_abs = chunk.tokens[span.start].index_abs
                        end_abs = chunk.tokens[span.end].index_abs
                        tokens = example.tokens[start_abs:end_abs + 1]
                        t_first = tokens[0]
                        t_last = tokens[-1]
                        text = example.text[t_first.span_rel.start:t_last.span_rel.end]
                        id_entity = 'T' + str(len(example.entities))
                        entity = Entity(
                            id=id_entity,
                            label=label,
                            text=text,
                            tokens=tokens,
                        )
                        example.entities.append(entity)

    def _get_feed_dict(self, examples: List[Example], mode: str):
        assert len(examples) > 0
        assert self.ner_enc is not None

        # bert
        input_ids = []
        input_mask = []
        segment_ids = []

        # ner
        first_pieces_coords = []
        num_pieces = []
        num_tokens = []
        ner_labels = []

        # filling
        for i, x in enumerate(examples):
            input_ids_i = []
            input_mask_i = []
            segment_ids_i = []
            first_pieces_coords_i = []

            # [CLS]
            input_ids_i.append(self.config["model"]["bert"]["cls_token_id"])
            input_mask_i.append(1)
            segment_ids_i.append(0)

            ner_labels_i = []
            ptr = 1

            # tokens
            for t in x.tokens:
                first_pieces_coords_i.append((i, ptr))
                num_pieces_ij = len(t.token_ids)
                input_ids_i += t.token_ids
                input_mask_i += [1] * num_pieces_ij
                segment_ids_i += [0] * num_pieces_ij
                if mode != ModeKeys.TEST:
                    label = t.labels[0]
                    id_label = self.ner_enc[label]
                    ner_labels_i.append(id_label)  # ner решается на уровне токенов!
                ptr += num_pieces_ij

            # [SEP]
            input_ids_i.append(self.config["model"]["bert"]["sep_token_id"])
            input_mask_i.append(1)
            segment_ids_i.append(0)

            # write
            num_pieces.append(len(input_ids_i))
            num_tokens.append(len(x.tokens))
            input_ids.append(input_ids_i)
            input_mask.append(input_mask_i)
            segment_ids.append(segment_ids_i)
            ner_labels.append(ner_labels_i)
            first_pieces_coords.append(first_pieces_coords_i)

        # padding
        pad_token_id = self.config["model"]["bert"]["pad_token_id"]
        pad_label_id = self.config["model"]["ner"]["no_entity_id"]
        num_tokens_max = max(num_tokens)
        num_pieces_max = max(num_pieces)
        for i in range(len(examples)):
            input_ids[i] += [pad_token_id] * (num_pieces_max - num_pieces[i])
            input_mask[i] += [0] * (num_pieces_max - num_pieces[i])
            segment_ids[i] += [0] * (num_pieces_max - num_pieces[i])
            ner_labels[i] += [pad_label_id] * (num_tokens_max - num_tokens[i])
            first_pieces_coords[i] += [(i, 0)] * (num_tokens_max - num_tokens[i])

        training = mode == ModeKeys.TRAIN

        d = {
            # bert
            self.input_ids_ph: input_ids,
            self.input_mask_ph: input_mask,
            self.segment_ids_ph: segment_ids,

            # ner
            self.first_pieces_coords_ph: first_pieces_coords,
            self.num_pieces_ph: num_pieces,
            self.num_tokens_ph: num_tokens,

            # common
            self.training_ph: training
        }

        if mode != ModeKeys.TEST:
            d[self.ner_labels_ph] = ner_labels

        return d

    def _set_placeholders(self):
        super()._set_placeholders()
        self.ner_labels_ph = tf.placeholder(dtype=tf.int32, shape=[None, None], name="ner_labels")

    def _set_loss(self):
        use_crf = self.config["model"]["ner"]["use_crf"]
        if use_crf:
            log_likelihood, _ = tf.contrib.crf.crf_log_likelihood(
                inputs=self.ner_logits_train,
                tag_indices=self.ner_labels_ph,
                sequence_lengths=self.num_tokens_ph,
                transition_params=self.transition_params
            )
            self.per_example_loss = -log_likelihood
            self.loss = tf.reduce_mean(self.per_example_loss)
        else:
            self.per_example_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(
                labels=self.ner_labels_ph, logits=self.ner_logits_train
            )
            self.loss = tf.reduce_mean(self.per_example_loss)

    def _build_ner_head_fn(self,  bert_out):
        """
        bert_out -> dropout -> stacked birnn (optional) -> dense(num_labels) -> crf (optional)
        :param bert_out:
        :return:
        """
        use_crf = self.config["model"]["ner"]["use_crf"]
        num_labels = self.config["model"]["ner"]["num_labels"]

        # dropout
        if (self.birnn_ner is None) or (self.config["model"]["ner"]["rnn"]["dropout"] == 0.0):
            x = self.bert_dropout(bert_out, training=self.training_ph)
        else:
            x = bert_out

        # birnn
        if self.birnn_ner is not None:
            sequence_mask = tf.sequence_mask(self.num_pieces_ph)
            x = self.birnn_ner(x, training=self.training_ph, mask=sequence_mask)

        # pieces -> tokens
        # сделано так для того, чтобы в ElmoJointModel не нужно было переопределять данный метод
        if self.first_pieces_coords_ph is not None:
            x = tf.gather_nd(x, self.first_pieces_coords_ph)  # [N, num_tokens_tokens, bert_dim or cell_dim * 2]

        # label logits
        logits = self.dense_ner_labels(x)

        # label ids
        if use_crf:
            with tf.variable_scope("crf", reuse=tf.AUTO_REUSE):
                transition_params = tf.get_variable("transition_params", [num_labels, num_labels], dtype=tf.float32)
            pred_ids, _ = tf.contrib.crf.crf_decode(logits, transition_params, self.num_tokens_ph)
        else:
            pred_ids = tf.argmax(logits, axis=-1)
            transition_params = None

        return logits, pred_ids, transition_params


class BertForNestedNER(BaseModelNER, BaseModelBert):
    def __init__(self, sess=None, config=None, ner_enc=None):
        super().__init__(sess=sess, config=config, ner_enc=ner_enc)

        # TENSORS
        self.tokens_pair_enc = None
        self.ner_logits_inference = None
        self.total_loss = None
        self.loss_denominator = None

        # LAYERS
        self.bert_dropout = None
        self.birnn_ner = None

    def _build_ner_head(self):
        self.bert_dropout = tf.keras.layers.Dropout(self.config["model"]["bert"]["dropout"])

        if self.config["model"]["ner"]["use_birnn"]:
            self.birnn_ner = StackedBiRNN(**self.config["model"]["ner"]["rnn"])

        self.tokens_pair_enc = GraphEncoder(**self.config["model"]["ner"]["biaffine"])

        self.ner_logits_train = self._build_ner_head_fn(bert_out=self.bert_out_train)
        self.ner_logits_inference = self._build_ner_head_fn(bert_out=self.bert_out_pred)

    def _set_placeholders(self):
        super()._set_placeholders()
        # [id_example, start, end, label]
        self.ner_labels_ph = tf.placeholder(dtype=tf.int32, shape=[None, 4], name="ner_labels")

    def _build_ner_head_fn(self,  bert_out):
        bert_out = self.bert_dropout(bert_out, training=self.training_ph)

        # pieces -> tokens
        x = tf.gather_nd(bert_out, self.first_pieces_coords_ph)  # [batch_size, num_tokens, bert_dim]

        if self.birnn_ner is not None:
            sequence_mask = tf.sequence_mask(self.num_tokens_ph)
            x = self.birnn_ner(x, training=self.training_ph, mask=sequence_mask)  # [N, num_tokens, cell_dim * 2]

        # encoding of pairs
        inputs = GraphEncoderInputs(head=x, dep=x)
        logits = self.tokens_pair_enc(inputs=inputs, training=self.training_ph)  # [N, num_tok, num_tok, num_entities]
        return logits

    def _set_loss(self, *args, **kwargs):
        """"
        1 1 1
        0 1 1
        0 0 1
        i - start, j - end
        """
        # per example loss
        no_entity_id = self.config["model"]["ner"]["no_entity_id"]
        logits_shape = tf.shape(self.ner_logits_train)
        labels_shape = logits_shape[:3]
        labels = get_dense_labels_from_indices(indices=self.ner_labels_ph, shape=labels_shape, no_label_id=no_entity_id)
        per_example_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(
            labels=labels, logits=self.ner_logits_train
        )  # [batch_size, num_tokens, num_tokens]

        # mask
        maxlen = logits_shape[1]
        span_mask = upper_triangular(maxlen, dtype=tf.float32)
        sequence_mask = tf.sequence_mask(self.num_tokens_ph, dtype=tf.float32)  # [batch_size, num_tokens]
        mask = span_mask[None, :, :] * sequence_mask[:, None, :] * sequence_mask[:, :, None]  # [batch_size, num_tokens, num_tokens]

        masked_per_example_loss = per_example_loss * mask
        total_loss = tf.reduce_sum(masked_per_example_loss)
        num_valid_spans = tf.cast(tf.reduce_sum(mask), tf.float32)
        self.loss = total_loss / num_valid_spans
        self.total_loss = total_loss
        self.loss_denominator = num_valid_spans

    def _get_feed_dict(self, examples: List[Example], mode: str):
        assert len(examples) > 0
        assert self.ner_enc is not None

        # bert
        input_ids = []
        input_mask = []
        segment_ids = []

        # ner
        first_pieces_coords = []
        num_pieces = []
        num_tokens = []
        ner_labels = []

        # filling
        for i, x in enumerate(examples):
            input_ids_i = []
            input_mask_i = []
            segment_ids_i = []
            first_pieces_coords_i = []

            # [CLS]
            input_ids_i.append(self.config["model"]["bert"]["cls_token_id"])
            input_mask_i.append(1)
            segment_ids_i.append(0)

            ptr = 1

            # tokens
            for t in x.tokens:
                assert len(t.token_ids) > 0
                first_pieces_coords_i.append((i, ptr))
                num_pieces_ij = len(t.token_ids)
                input_ids_i += t.token_ids
                input_mask_i += [1] * num_pieces_ij
                segment_ids_i += [0] * num_pieces_ij
                ptr += num_pieces_ij

            # [SEP]
            input_ids_i.append(self.config["model"]["bert"]["sep_token_id"])
            input_mask_i.append(1)
            segment_ids_i.append(0)

            # ner
            for entity in x.entities:
                assert entity.label is not None
                start = entity.tokens[0].index_rel
                assert start is not None
                end = entity.tokens[-1].index_rel
                assert end is not None
                id_label = self.ner_enc[entity.label]
                ner_labels.append((i, start, end, id_label))

            # write
            num_pieces.append(len(input_ids_i))
            num_tokens.append(len(x.tokens))
            input_ids.append(input_ids_i)
            input_mask.append(input_mask_i)
            segment_ids.append(segment_ids_i)
            first_pieces_coords.append(first_pieces_coords_i)

        # padding
        pad_token_id = self.config["model"]["bert"]["pad_token_id"]
        num_tokens_max = max(num_tokens)
        num_pieces_max = max(num_pieces)
        for i in range(len(examples)):
            input_ids[i] += [pad_token_id] * (num_pieces_max - num_pieces[i])
            input_mask[i] += [0] * (num_pieces_max - num_pieces[i])
            segment_ids[i] += [0] * (num_pieces_max - num_pieces[i])
            first_pieces_coords[i] += [(i, 0)] * (num_tokens_max - num_tokens[i])

        if len(ner_labels) == 0:
            ner_labels.append((0, 0, 0, 0))

        training = mode == ModeKeys.TRAIN

        d = {
            # bert
            self.input_ids_ph: input_ids,
            self.input_mask_ph: input_mask,
            self.segment_ids_ph: segment_ids,

            # ner
            self.first_pieces_coords_ph: first_pieces_coords,
            self.num_pieces_ph: num_pieces,
            self.num_tokens_ph: num_tokens,

            # common
            self.training_ph: training
        }

        if mode != ModeKeys.TEST:
            d[self.ner_labels_ph] = ner_labels

        return d

    # TODO: профилирование!!!
    def evaluate(self, examples: List[Example], **kwargs) -> Dict:
        chunks = []
        for x in examples:
            assert len(x.chunks) > 0
            chunks += x.chunks

        y_true_ner = []
        y_pred_ner = []

        total_loss = 0.0
        loss_denominator = 0
        no_entity_id = 0  # TODO: брать из конфига

        gen = batches_gen(
            examples=chunks,
            max_tokens_per_batch=self.config["inference"]["max_tokens_per_batch"],
            pieces_level=True
        )
        for batch in gen:
            feed_dict = self._get_feed_dict(batch, mode=ModeKeys.VALID)
            total_loss_i, d, ner_logits = self.sess.run([self.total_loss, self.loss_denominator, self.ner_logits_inference], feed_dict=feed_dict)
            total_loss += total_loss_i
            loss_denominator += d

            for i, x in enumerate(batch):
                # ner
                num_tokens = len(x.tokens)
                labels_true = np.full((num_tokens, num_tokens), no_entity_id, dtype=np.int32)

                for entity in x.entities:
                    start = entity.tokens[0].index_rel
                    end = entity.tokens[-1].index_rel
                    labels_true[start, end] = self.ner_enc[entity.label]

                labels_pred = np.full((num_tokens, num_tokens), no_entity_id, dtype=np.int32)
                ner_logits_i = ner_logits[i, :num_tokens, :num_tokens, :]
                spans_filtered = get_valid_spans(logits=ner_logits_i,  is_flat_ner=False)
                for span in spans_filtered:
                    labels_pred[span.start, span.end] = span.label

                y_true_ner += [self.inv_ner_enc[j] for j in labels_true.flatten()]
                y_pred_ner += [self.inv_ner_enc[j] for j in labels_pred.flatten()]

        # loss
        loss = total_loss / loss_denominator

        # ner
        ner_metrics_entity_level = classification_report(y_true=y_true_ner, y_pred=y_pred_ner, trivial_label="O")

        score = ner_metrics_entity_level["micro"]["f1"]
        performance_info = {
            "loss": loss,
            "score": score,
            "metrics": {
                "entity_level": ner_metrics_entity_level
            }
        }

        return performance_info

    # TODO: реалзиовать случай window > 1
    # TODO: копипаста в начале с BertForFlatNER
    def predict(self, examples: List[Example], **kwargs) -> None:
        """
        инференс. примеры не должны содержать разметку токенов и пар сущностей!
        сделано так для того, чтобы не было непредсказуемых результатов.

        ner - запись лейблов в Token.labels
        re - создание новых инстансов Arc и запись их в Example.arcs
        """
        # проверка примеров
        chunks = []
        for x in examples:
            assert len(x.chunks) > 0, f"[{x.id}] didn't split by chunks"
            for t in x.tokens:
                assert len(t.labels) == 0, f"[{x.id}] tokens are already annotated"
            for chunk in x.chunks:
                assert chunk.parent is not None, f"[{x.id}] parent for chunk {chunk.id} is not set. " \
                    f"It is not a problem, but must be set for clarity"
                chunks.append(chunk)

        id2example = {x.id: x for x in examples}
        assert len(id2example) == len(examples), f"examples must have unique ids, " \
            f"but got {len(id2example)} unique ids among {len(examples)} examples"

        gen = batches_gen(
            examples=chunks,
            max_tokens_per_batch=self.config["inference"]["max_tokens_per_batch"],
            pieces_level=True
        )
        for batch in gen:
            feed_dict = self._get_feed_dict(batch, mode=ModeKeys.TEST)
            ner_logits = self.sess.run(self.ner_logits_inference, feed_dict=feed_dict)

            for i, chunk in enumerate(batch):
                example = id2example[chunk.parent]
                num_tokens_i = len(chunk.tokens)

                ner_logits_i = ner_logits[i, :num_tokens_i, :num_tokens_i, :]
                spans_filtered = get_valid_spans(logits=ner_logits_i, is_flat_ner=False)
                for span in spans_filtered:
                    start_abs = chunk.tokens[span.start].index_abs
                    end_abs = chunk.tokens[span.end].index_abs
                    tokens = example.tokens[start_abs:end_abs + 1]
                    t_first = tokens[0]
                    t_last = tokens[-1]
                    text = example.text[t_first.span_abs.start:t_last.span_abs.end]
                    id_entity = 'T' + str(len(example.entities))
                    entity = Entity(
                        id=id_entity,
                        label=span.label,
                        text=text,
                        tokens=tokens,
                    )
                    example.entities.append(entity)