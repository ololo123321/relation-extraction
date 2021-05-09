from typing import Dict, List
from itertools import chain

import tensorflow as tf
import numpy as np

from src.data.base import Example, Entity
from src.data.postprocessing import get_valid_spans
from src.model.base import BaseModelRelationExtraction, BaseModelBert, ModeKeys
from src.model.layers import StackedBiRNN, GraphEncoder, GraphEncoderInputs
from src.model.utils import upper_triangular, get_entities_representation
from src.metrics import classification_report, classification_report_ner
from src.utils import get_entity_spans, batches_gen, get_filtered_by_length_chunks


# TODO: реализовать src.utils.get_entity_spans как тф-операцию, чтоб можно было за один форвард пасс
#  выводить спаны найденных сущностей

# вариенты векторизации сущностей:
# [start, end, attn]
# start + entity_label_emb

# if self.config["model"]["re"]["use_entity_emb"]:
#     num_labels = self.config["model"]["re"]["num_labels"]
#     if self.config["model"]["birnn"]["use"]:
#         emb_dim = self.config["model"]["birnn"]["params"]["cell_dim"] * 2
#     else:
#         emb_dim = self.config["model"]["bert"]["params"]["hidden_size"]
#     self.entity_emb = tf.keras.layers.Embedding(num_labels, emb_dim * 3)
#     if self.config["model"]["re"]["use_entity_emb_layer_norm"]:
#         self.ner_emb_layer_norm = tf.keras.layers.LayerNormalization()
#     self.entity_emb_dropout = tf.keras.layers.Dropout(self.config["model"]["re"]["entity_emb_dropout"])


class BertForRelationExtraction(BaseModelRelationExtraction, BaseModelBert):
    """
    сущности известны в виде [start, end, label]
    TODO: entity embeddings (code above)
    """
    def __init__(self, sess: tf.Session = None, config: Dict = None, ner_enc: Dict = None, re_enc: Dict = None):
        super().__init__(sess=sess, config=config, ner_enc=ner_enc, re_enc=re_enc)

        # PLACEHOLDERS
        self.ner_labels_ph = None
        self.re_labels_ph = None

        # TENSORS
        self.logits_train = None
        self.logits_pred = None
        self.num_entities = None
        self.total_loss = None
        self.loss_denominator = None

        # LAYERS
        self.entity_emb = None
        self.entity_emb_layer_norm = None
        self.entity_emb_dropout = None
        self.entity_pairs_enc = None

    def _build_re_head(self):
        self.logits_train, self.num_entities = self._build_re_head_fn(bert_out=self.bert_out_train)
        self.logits_pred, _ = self._build_re_head_fn(bert_out=self.bert_out_pred)

    def _set_placeholders(self):
        super()._set_placeholders()
        self.ner_labels_ph = tf.placeholder(tf.int32, shape=[None, 4], name="ner_labels")  # [i, start, end, label]
        self.re_labels_ph = tf.placeholder(tf.int32, shape=[None, 4], name="re_labels")  # [i, id_head, id_dep, label]

    def _set_layers(self):
        super()._set_layers()
        self.entity_pairs_enc = GraphEncoder(**self.config["model"]["re"]["biaffine"])

    def _build_re_head_fn(self,  bert_out):
        x = self._get_token_level_embeddings(bert_out=bert_out)  # [batch_size, num_tokens, D]

        # entity embeddings
        x, num_entities = get_entities_representation(
            x=x, ner_labels=self.ner_labels_ph, sparse_labels=True, ff_attn=None
        )  # [batch_size, num_ent, D * 3]
        inputs = GraphEncoderInputs(head=x, dep=x)
        logits = self.entity_pairs_enc(inputs, training=self.training_ph)  # [batch_size, num_ent, num_ent, num_rel]
        return logits, num_entities

    def _set_loss(self, *args, **kwargs):
        assert self.config["model"]["re"]["no_relation_id"] == 0
        logits_shape = tf.shape(self.logits_train)
        labels = tf.scatter_nd(
            indices=self.re_labels_ph[:, :-1], updates=self.re_labels_ph[:, -1], shape=logits_shape[:-1]
        )
        per_example_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(
            labels=labels, logits=self.logits_train
        )  # [batch_size, num_entities, num_entities]

        sequence_mask = tf.sequence_mask(self.num_entities, maxlen=logits_shape[1], dtype=tf.float32)
        mask = sequence_mask[:, None, :] * sequence_mask[:, :, None]

        masked_per_example_loss = per_example_loss * mask
        total_loss = tf.reduce_sum(masked_per_example_loss)
        num_pairs = tf.cast(tf.reduce_sum(self.num_entities ** 2), tf.float32)
        num_pairs = tf.maximum(num_pairs, 1.0)
        self.loss = total_loss / num_pairs

    def _get_feed_dict(self, examples: List[Example], mode: str) -> Dict:
        assert self.ner_enc is not None
        assert self.re_enc is not None

        # bert
        input_ids = []
        input_mask = []
        segment_ids = []

        # ner
        first_pieces_coords = []
        num_pieces = []
        num_tokens = []
        ner_labels = []

        # re
        re_labels = []

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

            # entities
            for entity in x.entities:
                start = entity.tokens[0].index_rel
                end = entity.tokens[-1].index_rel
                label = self.re_enc[entity.label]
                ner_labels.append((i, start, end, label))

            # relations
            if mode != ModeKeys.TEST:
                for arc in x.arcs:
                    assert arc.head_index is not None
                    assert arc.dep_index is not None
                    id_rel = self.re_enc[arc.rel]
                    re_labels.append((i, arc.head_index, arc.dep_index, id_rel))

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

        training = mode == ModeKeys.TRAIN

        d = {
            self.input_ids_ph: input_ids,
            self.input_mask_ph: input_mask,
            self.segment_ids_ph: segment_ids,
            self.first_pieces_coords_ph: first_pieces_coords,
            self.num_pieces_ph: num_pieces,
            self.num_tokens_ph: num_tokens,
            self.training_ph: training
        }

        if mode != ModeKeys.TEST:
            if len(ner_labels) == 0:
                ner_labels.append((0, 0, 0, 0))
            if len(re_labels) == 0:
                re_labels.append((0, 0, 0, 0))

            d[self.ner_labels_ph] = ner_labels
            d[self.re_labels_ph] = re_labels

        return d

    # TODO
    def evaluate(self, examples: List[Example], **kwargs) -> Dict:
        pass

    # TODO
    def predict(self, examples: List[Example], **kwargs) -> None:
        pass
