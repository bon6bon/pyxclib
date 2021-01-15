from numba import jit, njit, prange
from .sparse import retain_topk, _map
import scipy.sparse as sp
import numpy as np
import tqdm


@njit(parallel=True, nogil=True)
def _random_walk(q_rng, q_lbl, l_rng, l_qry, walk_to, p_reset, start, end):
    """
    Compute random walk for a batch of labels in the label space
    One hop is consits of following steps:
        1) Randomly jumping from label to a document 
        2) Randomly jumping from the document to a document 
    Arguments:
        ---------
        q_rng: label pointers in CSR format index pointer
               array of the matrix
        q_lbl: label indices in CSR format index array of 
               the matrix
        l_rng: document pointers in CSR format index pointer 
               array of the matrix
        l_qry: document indices in CSR format index pointer 
               array of the matrix
        walk_to: Random walk length (int)
        p_reset: Random restart probability (float)
        start: Start index of the label
        end: last index of the label

    Returns:
        ---------
        np.ndarray: np.int32 [start-end x walk_to] 
                    flattened array of indices for correlated
                    labels with duplicate entries corresponding 
                    to [start, ..., end] indices of the labels
        np.ndarray: np.float32 [start-end x walk_to] 
                    flattened array of relevance for correlated
                    labels with duplicate entries corresponding
                    to [start, ..., end] indices of the labels

    """
    n_nodes = end - start
    nbr_idx = np.empty((n_nodes, walk_to), dtype=np.int32)
    nbr_dat = np.empty((n_nodes, walk_to), dtype=np.float32)
    for idx in prange(0, n_nodes):
        lbl_k = idx + start
        p = 0
        for walk in np.arange(0, walk_to):
            if p < p_reset:
                l_start, l_end = l_rng[lbl_k], l_rng[lbl_k+1]
            else:
                _idx = nbr_idx[idx, walk-1]
                l_start, l_end = l_rng[_idx], l_rng[_idx+1]
            _s_query = l_qry[l_start: l_end]
            _qidx = np.random.choice(_s_query)
            q_start, q_end = q_rng[_qidx], q_rng[_qidx+1]
            nbr_idx[idx, walk] = np.random.choice(q_lbl[q_start: q_end])
            nbr_dat[idx, walk] = 1
            p = np.random.random()
    return nbr_idx.flatten(), nbr_dat.flatten()


class RandomWalk:
    """
    Class for RandomWalk simulation.
    Implementations include
        random walk over the label space
    Arguments:
        ---------
        trn_y: CSR matrix of NxL where L is the label space
        valid_labels: Label indices having atleast one training point
                      if passed None then it will compute it using trn_y
    """

    def __init__(self, trn_y, valid_labels=None):
        self.num_inst, self.num_lbls = trn_y.shape
        if valid_labels is None:
            valid_labels = np.where(np.ravel(trn_y.sum(axis=0) > 0))[0]
        self.valid_labels = valid_labels
        trn_y = trn_y.tocsc()[:, valid_labels].tocsr()
        valid_indices = np.where(np.ravel(trn_y.sum(axis=1)))[0]
        trn_y = trn_y[valid_indices].tocsr()
        self.trn_y = trn_y
        self.trn_y.sort_indices()
        self.trn_y.eliminate_zeros()

    def simulate(self, walk_to=50, p_reset=0.2, k=None, b_size=1000):
        """
        Perform random walk in batch to save memory
        Arguments:
            ----------
            walk_to: Random walk length
            p_reset: Restart probablity for random walk
            k: Retains only top-k most correlated labels
            b_size: Batch size to use for random walk
        Returns:
            ----------
            CSR Matrix: LxL dimensional random walk matrix
        """
        q_lbl = self.trn_y.indices
        q_rng = self.trn_y.indptr
        trn_y = self.trn_y.transpose().tocsr()
        trn_y.sort_indices()
        trn_y.eliminate_zeros()
        l_qry = trn_y.indices
        l_rng = trn_y.indptr
        n_lbs = self.trn_y.shape[1]
        zeros = 0
        mats = []
        for idx in tqdm.tqdm(np.arange(0, n_lbs, b_size)):
            start, end = idx, min(idx+b_size, n_lbs)
            cols, data = _random_walk(q_rng, q_lbl, l_rng, l_qry, walk_to,
                                      p_reset, start=start, end=end)
            rows = np.arange(end-start).reshape(-1, 1)
            rows = np.repeat(rows, walk_to, axis=1).flatten()
            mat = sp.coo_matrix((data, (rows, cols)), dtype=np.float32,
                                shape=(end-start, n_lbs))
            mat.sum_duplicates()
            mat = mat.tocsr()
            mat.sort_indices()
            diag = mat.diagonal(k=start)
            if k is not None:
                mat = retain_topk(mat.tocsr(), False, k)
            _diag = mat.diagonal(k=start)
            _diag[_diag == 0] = diag[_diag == 0]
            zeros += np.sum(_diag == 0)
            _diag[_diag == 0] = 1
            mat.setdiag(_diag, k=start)
            mats.append(mat)
            del rows, cols
        mats = sp.vstack(mats).tocsr()
        rows, cols = mats.nonzero()
        r_mat = sp.coo_matrix((mats.data, (rows, cols)), dtype=np.float32,
                              shape=(self.num_lbls, self.num_lbls))
        r_mat = _map(r_mat, self.valid_labels, axis=0, shape=r_mat.shape)
        r_mat = _map(r_mat, self.valid_labels, axis=1, shape=r_mat.shape)
        return r_mat.tocsr()