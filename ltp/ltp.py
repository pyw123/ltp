#! /usr/bin/env python
# -*- coding: utf-8 -*_
# Author: Yunlong Feng <ylfeng@ir.hit.edu.cn>
import os
import torch
import itertools
import regex as re
import transformers
from typing import Union, List

from packaging import version
from transformers import AutoTokenizer, cached_path, TensorType, BatchEncoding
from transformers.file_utils import is_remote_url

transformers_version = version.parse(transformers.__version__)

from ltp.models import Model
from ltp.utils import length_to_mask, eisner, split_sentence
from ltp.utils import get_entities
from ltp.utils import Trie

try:
    from torch.hub import _get_torch_home

    torch_cache_home = _get_torch_home()
except ImportError:
    torch_cache_home = os.path.expanduser(
        os.getenv("TORCH_HOME", os.path.join(os.getenv("XDG_CACHE_HOME", "~/.cache"), "torch"))
    )

default_cache_path = os.path.join(torch_cache_home, "ltp")
LTP_CACHE = os.getenv("LTP_CACHE", default_cache_path)

model_map = {
    'base': 'http://39.96.43.154/ltp/v2/base.tgz',
    'small': 'http://39.96.43.154/ltp/v2/small.tgz',
    'tiny': 'http://39.96.43.154/ltp/v2/tiny.tgz'
}


def no_gard(func):
    def wrapper(*args, **kwargs):
        with torch.no_grad():
            return func(*args, **kwargs)

    return wrapper


WORD_START = 'B-W'
WORD_MIDDLE = 'I-W'


def get_entities_with_list(labels_, itos):
    res = []
    for labels in labels_:
        labels = [itos[label] for label in labels]
        labels = get_entities(labels)
        res.append(labels)
    return res


def get_graph_entities(arcs, labels, itos):
    sequence_num = labels.size(0)
    arcs = torch.nonzero(arcs, as_tuple=False).cpu().detach().numpy().tolist()
    labels = labels.cpu().detach().numpy()

    res = [[] for _ in range(sequence_num)]
    for idx, arc_s, arc_e in arcs:
        label = labels[idx, arc_s, arc_e]
        res[idx].append((arc_s, arc_e, itos[label]))

    return res


def convert_idx_to_name(y, array_len, id2label):
    if id2label:
        return [[id2label[idx] for idx in row[:row_len]] for row, row_len in zip(y, array_len)]
    else:
        return [[idx for idx in row[:row_len]] for row, row_len in zip(y, array_len)]


class LTP(object):
    model: Model
    seg_vocab: List[str]
    pos_vocab: List[str]
    ner_vocab: List[str]
    dep_vocab: List[str]
    sdp_vocab: List[str]
    srl_vocab: List[str]

    tensor: TensorType = TensorType.PYTORCH

    def __init__(self, path: str = 'small', device=None, **kwargs):
        if device is not None:
            if isinstance(device, torch.device):
                self.device = device
            elif isinstance(device, str):
                self.device = torch.device(device)
        elif torch.cuda.is_available():
            self.device = torch.device('cuda')
        else:
            self.device = torch.device('cpu')

        if path in model_map or is_remote_url(path) or os.path.isfile(path):
            proxies = kwargs.pop("proxies", None)
            cache_dir = kwargs.pop("cache_dir", LTP_CACHE)
            force_download = kwargs.pop("force_download", False)
            resume_download = kwargs.pop("resume_download", False)
            local_files_only = kwargs.pop("local_files_only", False)
            path = cached_path(
                model_map.get(path, path),
                cache_dir=cache_dir,
                force_download=force_download,
                proxies=proxies,
                resume_download=resume_download,
                local_files_only=local_files_only,
                extract_compressed_file=True
            )
        elif not os.path.isdir(path):
            raise FileNotFoundError()
        ckpt = torch.load(os.path.join(path, "ltp.model"), map_location=self.device)
        ckpt['model_config']['init'].pop('pretrained', None)
        self.cache_dir = path
        self.model = Model.from_params(ckpt['model_config'], config=ckpt['pretrained_config']).to(self.device)
        self.model.load_state_dict(ckpt['model'], strict=transformers_version < version.parse("3.1.0"))
        self.model.eval()
        # todo fp16
        self.max_length = self.model.pretrained.config.max_position_embeddings
        self.seg_vocab = ckpt.get('seg', [WORD_START, WORD_MIDDLE])
        self.pos_vocab = ckpt.get('pos', [])
        self.ner_vocab = ckpt.get('ner', [])
        self.dep_vocab = ckpt.get('dep', [])
        self.sdp_vocab = ckpt.get('sdp', [])
        self.srl_vocab = [re.sub(r'ARG(\d)', r'A\1', tag.lstrip('ARGM-')) for tag in ckpt.get('srl', [])]
        self.tokenizer = AutoTokenizer.from_pretrained(path, config=self.model.pretrained.config, use_fast=True)
        self.trie = Trie()

        if kwargs.pop("need_config", False):
            config = ckpt['model_config']
            config['init']['seg']['vocab'] = self.seg_vocab
            config['init']['pos']['vocab'] = self.pos_vocab
            config['init']['ner']['vocab'] = self.ner_vocab
            config['init']['dep']['vocab'] = self.dep_vocab
            config['init']['sdp']['vocab'] = self.sdp_vocab
            config['init']['srl']['vocab'] = self.srl_vocab
            config['pretrained_config'] = ckpt['pretrained_config']
            self.config = config

    def __str__(self):
        return f"LTP {self.version} on {self.device}"

    def __repr__(self):
        return f"LTP {self.version} on {self.device}"

    @property
    def version(self):
        from ltp import __version__ as version
        return version

    def init_dict(self, path, max_window=None):
        self.trie.init(path, max_window)

    def add_words(self, words, max_window=4):
        self.trie.add_words(words)
        self.trie.max_window = max_window

    @staticmethod
    def sent_split(inputs: List[str], flag: str = "all", limit: int = 510):
        inputs = [split_sentence(text, flag=flag, limit=limit) for text in inputs]
        inputs = list(itertools.chain(*inputs))
        return inputs

    def seg_with_dict(self, inputs: List[str], tokenized: BatchEncoding, batch_prefix):
        # 进行正向字典匹配
        matching = []
        for source_text, encoding, preffix in zip(inputs, tokenized.encodings, batch_prefix):
            text = [source_text[start:end] for start, end in encoding.offsets[1:-1] if end != 0]
            matching_pos = self.trie.maximum_forward_matching(text, preffix)
            matching.append(matching_pos)
        return matching

    @no_gard
    def _seg(self, tokenizerd, is_preseged=False):
        input_ids = tokenizerd['input_ids'].to(self.device)
        attention_mask = tokenizerd['attention_mask'].to(self.device)
        token_type_ids = tokenizerd['token_type_ids'].to(self.device)
        length = torch.sum(attention_mask, dim=-1) - 2

        pretrained_output, *_ = self.model.pretrained(
            input_ids=input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids
        )

        # remove [CLS] [SEP]
        word_cls = pretrained_output[:, :1]
        char_input = torch.narrow(pretrained_output, 1, 1, pretrained_output.size(1) - 2)
        if is_preseged:
            segment_output = None
        else:
            segment_output = torch.argmax(self.model.seg_decoder(char_input), dim=-1).cpu().numpy()
        return word_cls, char_input, segment_output, length

    @no_gard
    def seg(self, inputs: Union[List[str], List[List[str]]], truncation: bool = True, is_preseged=False):
        """
        分词

        Args:
            inputs: 句子列表
            truncation: 是否对过长的句子进行截断，如果为 False 可能会抛出异常
            is_preseged:  是否已经进行过分词

        Returns:
            words: 分词后的序列
            hidden: 用于其他任务的中间表示
        """
        tokenized = self.tokenizer.batch_encode_plus(
            inputs, padding=True, truncation=truncation,
            return_tensors=self.tensor, max_length=self.max_length,
            is_pretokenized=is_preseged
        )
        cls, hidden, seg, lengths = self._seg(tokenized, is_preseged=is_preseged)

        batch_prefix = [[
            word_idx != encoding.words[idx - 1]
            for idx, word_idx in enumerate(encoding.words) if word_idx is not None
        ] for encoding in tokenized.encodings]

        # merge segments with maximum forward matching
        if self.trie.is_init and not is_preseged:
            matches = self.seg_with_dict(inputs, tokenized, batch_prefix)
            for sent_match, sent_seg in zip(matches, seg):
                for start, end in sent_match:
                    sent_seg[start] = 0
                    sent_seg[start + 1:end] = 1
                    if end < len(sent_seg):
                        sent_seg[end] = 0

        if is_preseged:
            sentences = inputs
            word_length = [len(sentence) for sentence in sentences]

            word_idx = []
            for encodings in tokenized.encodings:
                sentence_word_idx = []
                for idx, (start, end) in enumerate(encodings.offsets[1:]):
                    if start == 0 and end != 0:
                        sentence_word_idx.append(idx)
                word_idx.append(torch.as_tensor(sentence_word_idx, device=self.device))
        else:
            segment_output = convert_idx_to_name(seg, lengths, self.seg_vocab)
            sentences = []
            word_idx = []
            word_length = []

            for source_text, length, encoding, seg_tag, preffix in \
                    zip(inputs, lengths, tokenized.encodings, segment_output, batch_prefix):
                words = encoding.words[1:length + 1]
                offsets = encoding.offsets[1:length + 1]
                text = [source_text[start:end] for start, end in offsets]

                for idx in range(1, length):
                    current_beg = offsets[idx][0]
                    forward_end = offsets[idx - 1][-1]
                    if forward_end < current_beg:
                        text[idx] = source_text[forward_end:current_beg] + text[idx]
                    if not preffix[idx]:
                        seg_tag[idx] = WORD_MIDDLE

                entities = get_entities(seg_tag)
                word_length.append(len(entities))
                sentences.append([''.join(text[entity[1]:entity[2] + 1]).strip() for entity in entities])
                word_idx.append(torch.as_tensor([entity[1] for entity in entities], device=self.device))

        word_idx = torch.nn.utils.rnn.pad_sequence(word_idx, batch_first=True)
        word_idx = word_idx.unsqueeze(-1).expand(-1, -1, hidden.shape[-1])  # 展开

        word_input = torch.gather(hidden, dim=1, index=word_idx)  # 每个word第一个char的向量

        word_cls_input = torch.cat([cls, word_input], dim=1)
        word_cls_mask = length_to_mask(torch.as_tensor(word_length, device=self.device) + 1)
        word_cls_mask[:, 0] = False  # ignore the first token of each sentence
        return sentences, {
            'word_cls': cls, 'word_input': word_input, 'word_length': word_length,
            'word_cls_input': word_cls_input, 'word_cls_mask': word_cls_mask
        }

    @no_gard
    def pos(self, hidden: dict):
        """
        词性标注
        Args:
            hidden: 分词时所得到的中间表示

        Returns:
            pos: 词性标注结果
        """
        postagger_output = self.model.pos_decoder(hidden['word_input'], hidden['word_length'])
        postagger_output = torch.argmax(postagger_output, dim=-1).cpu().numpy()
        postagger_output = convert_idx_to_name(postagger_output, hidden['word_length'], self.pos_vocab)
        return postagger_output

    @no_gard
    def ner(self, hidden: dict):
        """
        命名实体识别
        Args:
            hidden: 分词时所得到的中间表示

        Returns:
            pos: 命名实体识别结果
        """
        word_length = torch.as_tensor(hidden['word_length'], device=self.device)
        ner_output = self.model.ner_decoder(hidden['word_input'], word_length)
        ner_output = torch.argmax(ner_output, dim=-1).cpu().numpy()
        ner_output = convert_idx_to_name(ner_output, hidden['word_length'], self.ner_vocab)
        return [get_entities(ner) for ner in ner_output]

    @no_gard
    def srl(self, hidden: dict, keep_empty=True):
        """
        语义角色标注
        Args:
            hidden: 分词时所得到的中间表示

        Returns:
            pos: 语义角色标注结果
        """
        word_length = torch.as_tensor(hidden['word_length'], device=hidden['word_input'].device)
        word_mask = length_to_mask(word_length)
        srl_output, srl_length, crf = self.model.srl_decoder(hidden['word_input'], hidden['word_length'])
        mask = word_mask.unsqueeze_(-1).expand(-1, -1, word_mask.size(1))
        mask = (mask & mask.transpose(-1, -2)).flatten(end_dim=1)
        index = mask[:, 0]
        mask = mask[index]

        srl_input = srl_output.flatten(end_dim=1)[index]
        srl_entities = crf.decode(torch.log_softmax(srl_input, dim=-1), mask)
        srl_entities = get_entities_with_list(srl_entities, self.srl_vocab)

        srl_labels_res = []
        for length in srl_length:
            srl_labels_res.append([])
            curr_srl_labels, srl_entities = srl_entities[:length], srl_entities[length:]
            srl_labels_res[-1].extend(curr_srl_labels)

        if not keep_empty:
            srl_labels_res = [[(idx, labels) for idx, labels in enumerate(srl_labels) if len(labels)]
                              for srl_labels in srl_labels_res]
        return srl_labels_res

    @no_gard
    def dep(self, hidden: dict, fast=False):
        """
        依存句法树
        Args:
            hidden: 分词时所得到的中间表示
            fast: 启用 fast 模式时，减少对结果的约束，速度更快，相应的精度会降低

        Returns:
            依存句法树结果
        """
        dep_arc, dep_label, word_length = self.model.dep_decoder(hidden['word_cls_input'], hidden['word_length'])
        if fast:
            dep_arc_fix = dep_arc.argmax(dim=-1).unsqueeze_(-1).expand_as(dep_arc)
        else:
            dep_arc_fix = eisner(dep_arc, hidden['word_cls_mask']).unsqueeze_(-1).expand_as(dep_arc)
        dep_arc = torch.zeros_like(dep_arc, dtype=torch.bool).scatter_(-1, dep_arc_fix, True)
        dep_label = torch.argmax(dep_label, dim=-1)

        word_cls_mask = hidden['word_cls_mask']
        word_cls_mask = word_cls_mask.unsqueeze(-1).expand(-1, -1, word_cls_mask.size(1))
        word_cls_mask = word_cls_mask & word_cls_mask.transpose(-1, -2)
        for si, length in enumerate(hidden['word_length']):
            word_cls_mask[si, :length, 0] = True
        dep_arc = dep_arc & word_cls_mask
        dep_label = get_graph_entities(dep_arc, dep_label, self.dep_vocab)

        return dep_label

    @no_gard
    def sdp(self, hidden: dict, graph=True):
        """
        语义依存图（树）
        Args:
            hidden: 分词时所得到的中间表示
            graph: 选择是语义依存图还是语义依存树结果

        Returns:
            语义依存图（树）结果
        """
        sdp_arc, sdp_label, _ = self.model.sdp_decoder(hidden['word_cls_input'], hidden['word_length'])
        sdp_arc = torch.sigmoid_(sdp_arc)

        # 避免自指
        eye = torch.arange(0, sdp_arc.size(1), device=sdp_arc.device).view(1, 1, -1).expand(sdp_arc.size(0), -1, -1)
        sdp_arc.scatter_(1, eye, 0)

        if graph:
            # 语义依存图
            sdp_arc.transpose_(-1, -2)
            sdp_root_mask = sdp_arc[:, 0].argmax(dim=-1).unsqueeze_(-1).expand_as(sdp_arc[:, 0])
            sdp_arc[:, 0] = 0
            sdp_arc[:, 0].scatter_(dim=-1, index=sdp_root_mask, value=1)
            sdp_arc_T = sdp_arc.transpose(-1, -2)
            sdp_arc_fix = sdp_arc_T.argmax(dim=-1).unsqueeze_(-1).expand_as(sdp_arc)
            sdp_arc = ((sdp_arc_T > 0.5) & (sdp_arc_T > sdp_arc)).scatter_(-1, sdp_arc_fix, True)
        else:
            # 语义依存树
            sdp_arc_fix = eisner(sdp_arc, hidden['word_cls_mask']).unsqueeze_(-1).expand_as(sdp_arc)
            sdp_arc = torch.zeros_like(sdp_arc, dtype=torch.bool).scatter_(-1, sdp_arc_fix, True)

        sdp_label = torch.argmax(sdp_label, dim=-1)

        word_cls_mask = hidden['word_cls_mask']
        word_cls_mask = word_cls_mask.unsqueeze(-1).expand(-1, -1, word_cls_mask.size(1))
        word_cls_mask = word_cls_mask & word_cls_mask.transpose(-1, -2)
        for si, length in enumerate(hidden['word_length']):
            word_cls_mask[si, :length, 0] = True
        sdp_arc = sdp_arc & word_cls_mask
        sdp_label = get_graph_entities(sdp_arc, sdp_label, self.sdp_vocab)

        return sdp_label
