''' Define the Transformer model '''
import torch
import torch.nn as nn
import numpy as np
import transformer.Constants as Constants
from transformer.Modules import BottleLinear as Linear
from transformer.Layers import EncoderLayer, DecoderLayer, DecoderStepLayer
from basic_pytorch.gpu_utils import to_gpu, FloatTensor, LongTensor, ByteTensor

__author__ = "Yu-Hsiang Huang and Egor Kraev"

def position_encoding_init(n_position, d_pos_vec):
    ''' Init the sinusoid position encoding table '''

    # keep dim 0 for padding token position encoding zero vector
    position_enc = np.array([
        [pos / np.power(10000, 2 * (j // 2) / d_pos_vec) for j in range(d_pos_vec)]
        if pos != 0 else np.zeros(d_pos_vec) for pos in range(n_position)])

    position_enc[1:, 0::2] = np.sin(position_enc[1:, 0::2]) # dim 2i
    position_enc[1:, 1::2] = np.cos(position_enc[1:, 1::2]) # dim 2i+1
    return torch.from_numpy(position_enc).type(torch.FloatTensor)

def get_attn_padding_mask(seq_q, seq_k, num_actions=Constants.PAD):
    ''' Indicate the padding-related part to mask '''
    assert seq_q.dim() == 2 and seq_k.dim() == 2
    mb_size, len_q = seq_q.size()
    mb_size, len_k = seq_k.size()
    pad_attn_mask = seq_k.data.eq(num_actions).unsqueeze(1)   # bx1xsk
    pad_attn_mask = pad_attn_mask.expand(mb_size, len_q, len_k) # bxsqxsk
    return pad_attn_mask



# class Decoder(nn.Module):
#     ''' A decoder model with self attention mechanism. '''
#     def __init__(
#             self, n_tgt_vocab, n_max_seq, n_layers=6, n_head=8, d_k=64, d_v=64,
#             d_word_vec=512, d_model=512, d_inner_hid=1024, dropout=0.1):
#
#         super(Decoder, self).__init__()
#         n_position = n_max_seq + 1
#         self.n_max_seq = n_max_seq
#         self.d_model = d_model
#
#         self.position_enc = nn.Embedding(
#             n_position, d_word_vec, padding_idx=Constants.PAD)
#         self.position_enc.weight.data = position_encoding_init(n_position, d_word_vec)
#
#         self.tgt_word_emb = nn.Embedding(
#             n_tgt_vocab, d_word_vec, padding_idx=Constants.PAD)
#         self.dropout = nn.Dropout(dropout)
#
#         self.layer_stack = nn.ModuleList([
#             DecoderLayer(d_model, d_inner_hid, n_head, d_k, d_v, dropout=dropout)
#             for _ in range(n_layers)])
#
#     def forward(self, tgt_seq, tgt_pos, src_seq, enc_output, return_attns=False):
#         # Word embedding look up
#         dec_input = self.tgt_word_emb(tgt_seq)
#
#         # Position Encoding addition
#         dec_input += self.position_enc(tgt_pos)
#
#         # Decode
#         dec_slf_attn_pad_mask = get_attn_padding_mask(tgt_seq, tgt_seq)
#         dec_slf_attn_sub_mask = get_attn_subsequent_mask(tgt_seq)
#         dec_slf_attn_mask = torch.gt(dec_slf_attn_pad_mask + dec_slf_attn_sub_mask, 0)
#
#         dec_enc_attn_pad_mask = get_attn_padding_mask(tgt_seq, src_seq)
#
#         if return_attns:
#             dec_slf_attns, dec_enc_attns = [], []
#
#         dec_output = dec_input
#         for dec_layer in self.layer_stack:
#             dec_output, dec_slf_attn, dec_enc_attn = dec_layer(
#                 dec_output, enc_output,
#                 slf_attn_mask=dec_slf_attn_mask,
#                 dec_enc_attn_mask=dec_enc_attn_pad_mask)
#
#             if return_attns:
#                 dec_slf_attns += [dec_slf_attn]
#                 dec_enc_attns += [dec_enc_attn]
#
#         if return_attns:
#             return dec_output, dec_slf_attns, dec_enc_attns
#         else:
#             return dec_output,

class SelfAttentionDecoderStep(nn.Module):
    ''' One continuous step of the decoder '''
    def __init__(self,
                 num_actions,
                 max_seq_len,
                 n_layers=2,
                 n_head=3,#8,
                 d_k=8,#64,
                 d_v=8,#64,
                 d_model=32,#512,
                 d_inner_hid=32,#1024,
                 drop_rate=0.1):

        super().__init__()
        n_position = max_seq_len + 1 # Why the +1? Because of the dummy prev action for first step
        self.n_max_seq = max_seq_len
        self.d_model = d_model

        ## TODO: why not make it an explicit function, don't have to worry about non-trainable params
        self.position_enc = nn.Embedding(n_position, d_model, padding_idx=Constants.PAD)
        self.position_enc.weight.data = position_encoding_init(n_position, d_model)
        self.position_enc.weight.requires_grad = False # will this suffice to make them not trainable?


        # TODO: do we want relu after embedding? Probably not; make consistent
        self.embedder = nn.Embedding(
            num_actions, d_model, padding_idx=num_actions-1) # Assume the padding index is the max possible?
        self.dropout = nn.Dropout(drop_rate)

        self.layer_stack = nn.ModuleList([
            DecoderStepLayer(d_model, d_inner_hid, n_head, d_k, d_v, dropout=drop_rate)
            for _ in range(n_layers)])

        self.enc_output_transform = None # will need to convert encoder output to d_model
        self.dec_output_transform = to_gpu(nn.Linear(self.d_model, num_actions))
        self.all_actions = None

    def encode(self, last_action, last_action_pos):
        '''
        Encode the input (last action) into a vector
        :param last_action: batch x 1, batch of sequences of length 1 of ints
        :param last_action_pos: int
        :return: FloatTensor batch x num_actions
        '''
        if last_action_pos >= 0:  # if we're not at step 0
            # Word embedding look up
            dec_input = self.embedder(last_action)
            # Position Encoding addition
            batch_size = last_action.size()[0]
            pos_enc = self.position_enc(
                                            (torch.ones(1,1)*last_action_pos).type(LongTensor)
                                        ).expand(batch_size,1,self.d_model)
            dec_input += pos_enc
        else: # just return
            dec_input = torch.zeros_like(self.embedder(torch.zeros_like(last_action,)))

        return dec_input

    def forward(self, enc_output, last_action, last_action_pos=None, src_seq=None, return_attns=False):
        '''
        Does one continuous step of the decoder, waiting for a policy to then pick an action from
        its output and call it again
        :param last_action: batch of ints: last action taken
        :param last_action_pos: int: num of steps since last reset
        :param enc_output: either batch x z_size or batch x max_input_seq_len x z_size, encoder output
        :param src_seq: if enc_output is 2-dim, ignored; else used to check for padding, to make padding mask
        :param return_attns:
        :return:
        '''

        if enc_output.size()[-1] != self.d_model: # make sure encoder output has correct dim
            if self.enc_output_transform is None:
                self.enc_output_transform = to_gpu(nn.Linear(enc_output.size()[-1], self.d_model))
            enc_output = self.enc_output_transform(enc_output)

        if last_action_pos >=0:
            last_action = (last_action.unsqueeze(1)).type(LongTensor)
        else:
            last_action = ((torch.ones(len(last_action), 1)) * -1).type(LongTensor)

        if self.all_actions is None:
            self.all_actions = last_action
        else:
            self.all_actions = torch.cat([self.all_actions,last_action], dim=1)

        if len(enc_output.shape) == 2: # this is the case we support at the moment
            enc_output = torch.unsqueeze(enc_output, 1)  # make encoded vector look like a sequence
            # TODO: check that mask convention is 1 = mask, 0=leave
            # as each enc_input sequence has length 1, don't need to mask
            dec_enc_attn_pad_mask = torch.zeros(enc_output.size()[0],1,1).type(ByteTensor)
        else:
            raise NotImplementedError()
            #dec_enc_attn_pad_mask = get_attn_padding_mask(last_action, src_seq)

        dec_slf_attn_pad_mask = get_attn_padding_mask(last_action, self.all_actions)

        dec_input = self.encode(last_action, last_action_pos)

        if return_attns:
            dec_slf_attns, dec_enc_attns = [], []

        dec_output = dec_input
        for dec_layer in self.layer_stack:
            dec_output, dec_slf_attn, dec_enc_attn = dec_layer(
                dec_output, enc_output,
                slf_attn_mask=dec_slf_attn_pad_mask,
                dec_enc_attn_mask=dec_enc_attn_pad_mask)

            if return_attns:
                dec_slf_attns += [dec_slf_attn]
                dec_enc_attns += [dec_enc_attn]

        # As the output 'sequence' only contains one step, get rid of that dimension
        if return_attns:
            return dec_output[:,0,:], dec_slf_attns, dec_enc_attns
        else:
            return self.dec_output_transform(dec_output[:,0,:])

    def reset_state(self):
        self.all_actions = None
        for m in self.layer_stack:
            m.reset_state()

# class Transformer(nn.Module):
#     ''' A sequence to sequence model with attention mechanism. '''
#
#     def __init__(
#             self, n_src_vocab, n_tgt_vocab, n_max_seq, n_layers=6, n_head=8,
#             d_word_vec=512, d_model=512, d_inner_hid=1024, d_k=64, d_v=64,
#             dropout=0.1, proj_share_weight=True, embs_share_weight=True):
#
#         super(Transformer, self).__init__()
#         self.encoder = Encoder(
#             n_src_vocab, n_max_seq, n_layers=n_layers, n_head=n_head,
#             d_word_vec=d_word_vec, d_model=d_model,
#             d_inner_hid=d_inner_hid, dropout=dropout)
#         self.decoder = Decoder(
#             n_tgt_vocab, n_max_seq, n_layers=n_layers, n_head=n_head,
#             d_word_vec=d_word_vec, d_model=d_model,
#             d_inner_hid=d_inner_hid, dropout=dropout)
#         self.tgt_word_proj = Linear(d_model, n_tgt_vocab, bias=False)
#         self.dropout = nn.Dropout(dropout)
#
#         assert d_model == d_word_vec, \
#         'To facilitate the residual connections, \
#          the dimensions of all module output shall be the same.'
#
#         if proj_share_weight:
#             # Share the weight matrix between tgt word embedding/projection
#             assert d_model == d_word_vec
#             self.tgt_word_proj.weight = self.decoder.tgt_word_emb.weight
#
#         if embs_share_weight:
#             # Share the weight matrix between src/tgt word embeddings
#             # assume the src/tgt word vec size are the same
#             assert n_src_vocab == n_tgt_vocab, \
#             "To share word embedding table, the vocabulary size of src/tgt shall be the same."
#             self.encoder.src_word_emb.weight = self.decoder.tgt_word_emb.weight
#
#     #TODO: do we need this? Or just set requires_grad to False, or not even register them as params!
#     def get_trainable_parameters(self):
#         ''' Avoid updating the position encoding '''
#         enc_freezed_param_ids = set(map(id, self.encoder.position_enc.parameters()))
#         dec_freezed_param_ids = set(map(id, self.decoder.position_enc.parameters()))
#         freezed_param_ids = enc_freezed_param_ids | dec_freezed_param_ids
#         return (p for p in self.parameters() if id(p) not in freezed_param_ids)
#
#     def forward(self, src, tgt):
#         src_seq, src_pos = src
#         tgt_seq, tgt_pos = tgt
#
#         tgt_seq = tgt_seq[:, :-1]
#         tgt_pos = tgt_pos[:, :-1]
#
#         enc_output, *_ = self.encoder(src_seq, src_pos)
#         dec_output, *_ = self.decoder(tgt_seq, tgt_pos, src_seq, enc_output)
#         seq_logit = self.tgt_word_proj(dec_output)
#
#         return seq_logit.view(-1, seq_logit.size(2))
