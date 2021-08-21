#!/usr/bin/env python
# -*- coding: utf-8 -*-


import logging
import numpy             as     np
from   mpi4py            import MPI
from   mpi4py.util.dtlib import from_numpy_dtype


PTR_BUFF_SIZE = 3 # PTR, MAX, LEN


def make_win(dtype, n_buf, comm, host):
    """
    make_win(dtype, n_buf, comm, host)

    Create an MPI RMA window containing elements of MPI type `dtype`. The RMA
    window has length `n_buf`. It is accessible on MPI Communicator `comm`, and
    is hosted on rank `host`.
    """
    itemsize = dtype.Get_size()
    win_size = n_buf * itemsize if comm.Get_rank() == host else 0
    return MPI.Win.Allocate(
        size      = win_size,
        disp_unit = itemsize,
        comm      = comm
    )



class FrameBuffer(object):

    @staticmethod
    def logger_name(rank):
        """
        FrameBuffer.logger_name(rank)

        Provide name of Logger for MPI rank `rank`.
        """
        return __name__ + f"::FrameBuffer.{rank}.log"


    def __init__(self, n_buf, n_mes, dtype=np.float64, host=0):
        """
        FrameBuffer(n_buf, n_mes, dtype=np.float64, host=0)

        Frame Buffers are a Message Queue. Each 'frame' refers to an
        ecapsulation of a message in the message queue (called a 'buffer').
        Each frame contains a counter representing the true message size, and a
        claim ID which ensures that the subsequent frames are claimed by the
        same MPI rank. This constructor preallocates MPI RMA window consisting
        of `n_buf` messages. Each message is `dtype`. The RMA buffer is hosted
        on rank `host`.

        A logger is available when the logging level is set to `logging.DEBUG`
        """
        self.comm = MPI.COMM_WORLD
        self.rank = self.comm.Get_rank()
        self.host = host

        self.np_dtype  = np.dtype(
            [('end', np.uint64,), ('m', np.uint64,), ('f', dtype, (n_mes,))]
        )
        self.mpi_dtype = from_numpy_dtype(self.np_dtype)
        self.mpi_dtype.Commit()

        self.n_buf = n_buf
        self.n_mes = n_mes

        self.win = make_win(
            dtype = self.mpi_dtype,
            n_buf = self.n_buf,
            comm  = self.comm,
            host  = host
        )

        self.ptr = make_win(
            dtype = from_numpy_dtype(np.uint64),
            n_buf = PTR_BUFF_SIZE,
            comm  = self.comm,
            host  = host
        )

        self.log = logging.getLogger(FrameBuffer.logger_name(self.rank))

        if self.rank == self.host:
            self.lock()
            self._ptr_put((0, 0, 0))
            self.unlock()


    def __del__(self):
        self.mpi_dtype.Free()


    def lock(self):
        """
        lock()

        Lock the Frame Buffer's MPI RMA windows. Locks are MPI.LOCK_EXCLUSIVE.
        """
        self.win.Lock(rank=self.host, lock_type=MPI.LOCK_EXCLUSIVE)
        self.ptr.Lock(rank=self.host, lock_type=MPI.LOCK_EXCLUSIVE)

        self.log.debug("lock")


    def unlock(self):
        """
        unlock()

        Unlocks the Frame Buffer's MPI RMA windows.
        """
        self.win.Unlock(rank=self.host)
        self.ptr.Unlock(rank=self.host)

        self.log.debug(f"unlock")


    @property
    def idx(self):
        """
        Index to the current frame
        """
        return self._idx


    @property
    def max(self):
        """
        Maximum index in the current buffer
        """
        return self._max


    @property
    def len(self):
        """
        Maximum length of the all data entered into the buffer
        """
        return self._len


    def take(self, N):
        """
        take(N)

        Take (claim) `N` frames from the buffer, and increment counters.

        Requires a lock.
        """
        self.incr(N, 0, 0)
        # print(f"take {self.rank=} {self.idx=} {self.max=} {self.len=}")
        [buf] = self._buf_get(N, self.idx)
        # print(f"{buf=}")
        return buf['f'][:buf['end']]


    def put(self, src):
        """
        put(src)

        Place `src` at the end of the buffer.

        Requires a lock.
        """
        self.incr(0, 1, 0)
        # print(f"put {self.rank=} {self.idx=} {self.max=} {self.len=}")
        if self.rank == self.host:
            self._buf_set(src, self.max)
        else:
            buf = np.empty(1, dtype=self.np_dtype)
            buf[0]['end'] = len(src)
            buf[0]['f'][:len(src)] = src[:]

            # print(f"{buf=}")
            self._buf_put(buf, self.max)


    def sync(self):
        """
        sync()

        Syncronizes the local pointer states:
            * idx: index to the current frame
            * max: maximum index in the current buffer
            * len: maximum length of the all data entered into the buffer

        Requires a lock.
        """
        [self._idx, self._max, self._len] = self._ptr_get()


    def init(self, idx, idx_max, idx_len):
        """
        init(idx, idx_max, idx_len)

        Set the local pointer states and syncronize the MPI RMA windows.

        Requires a lock.
        """
        self._ptr_put((idx, idx_max, idx_len))


    def incr(self, idx, idx_max, idx_len):
        """
        incr()

        Increments the remote and local pointers.

        Requires a lock.
        """
        [self._idx, self._max, self._len] = self._ptr_incr(
            (idx, idx_max, idx_len)
        )


    def _ptr_put(self, src):
        """
        _ptr_put(src)

        Set the MPI RMA window to the state in src.
        """
        buf = np.empty(PTR_BUFF_SIZE, dtype=np.uint64)

        buf[:] = src[:]

        self.ptr.Put(buf, target_rank=self.host)

        self.log.debug(f"_ptr_put {src=}")


    def _ptr_incr(self, src):
        """
        _ptr_incr(src)

        Increment the state in the MPI RMA window by src.
        """
        buf  = np.empty(PTR_BUFF_SIZE, dtype=np.uint64)
        incr = np.empty(PTR_BUFF_SIZE, dtype=np.uint64)

        incr[:] = src[:]
        self.ptr.Get_accumulate(incr, buf, target_rank=self.host)

        self.log.debug(f"_ptr_incr {buf=} {incr=}")

        return buf


    def _ptr_get(self):
        """
        _ptr_get():

        Read the state in the MPI RMA window.
        """
        buf = np.empty(PTR_BUFF_SIZE, dtype=np.uint64)

        self.ptr.Get(buf, target_rank=self.host)

        self.log.debug(f"_ptr_get {buf=}")

        return buf


    def _buf_get(self, N, offset):
        """
        _buf_get(N, offset)

        Get `N` frames starting at index `offset`
        """
        buf = np.empty(N, dtype=self.np_dtype)

        self.win.Get(
            [buf, self.mpi_dtype],
            target_rank = self.host,
            target      = (offset % self.n_buf, N, self.mpi_dtype)
        )

        self.log.debug(f"_buf_get {offset=}")

        return buf


    def _buf_set(self, src, idx):
        """
        _buf_set(src, idx)

        Set `src` at the location at `idx`.
        """
        mem = np.frombuffer(self.win, dtype = self.np_dtype)
        # print(f"{mem=}, {src=}, {idx=}, {self.n_buf=} {int(idx) % self.n_buf}")
        mem[int(idx % self.n_buf)]['end'] = len(src)
        mem[int(idx % self.n_buf)]['f'][:len(src)] = src[:]

        self.log.debug(f"_buf_set {idx=}")


    def _buf_put(self, src, offset):
        """
        _buf_put(src)

        Put `src` into the MPI RMA window to the state at `offset`.
        """
        buf = np.empty(len(src), dtype=self.np_dtype)

        buf[:] = src[:]

        self.win.Put(
            [buf, self.mpi_dtype],
            target_rank = self.host,
            target      = (offset % self.n_buf, len(src), self.mpi_dtype)
        )

        self.log.debug(f"_buf_put {offset=}")


    def buf_fill(self, src, offset):
        """
        """
        idx_remain = len(src) - offset
        idx_max = self.n_buf if idx_remain > self.n_buf else idx_remain

        mem = np.frombuffer(self.win, dtype = self.np_dtype)
        mem[:idx_max] = src[offset:offset + idx_max]

        self.log.debug(f"buf_fill {idx_max=} {offset=}")
        return idx_max


    def fence(self):
        """
        fence()

        Place a Fence into the MPI RMA Windows.
        """
        self.win.Fence()
        self.ptr.Fence()


class Producer(object):

    def __init__(self, n_buf, n_mes, single=False):
        self.comm = MPI.COMM_WORLD
        self.rank = self.comm.Get_rank()

        self.mpi_dtype = MPI.DOUBLE
        self.np_dtype  = np.float64
        if single:
            self.mpi_dtype = MPI.FLOAT
            self.np_dtype = np.float32

        self.buf = FrameBuffer(n_buf, n_mes, dtype=self.np_dtype, host=0)


    def put(self, src):
        while True:
            self.buf.lock()
            self.buf.sync()

            # Check if there is space in the buffer for new elements. If the
            # buffer is full, spin and watch for space
            # print(f"putting {self.buf.idx=} {self.buf.max=} {self.buf.len=}")
            if self.buf.max - self.buf.idx >= self.buf.n_buf:
                self.buf.unlock()
                continue

            self.buf.put(src)
            self.buf.unlock()
            return


    def claim(self, N):
        if self.rank == 0:
            self.buf.lock()
            self.buf.incr(0, 0, N)
            self.buf.sync()
            # print(f"claim: {self.buf.idx=} {self.buf.max=} {self.buf.len=}")
            self.buf.unlock()
        else:
            return


    def fill(self, src):

        if self.rank == 0:

            self.buf.lock()
            chunk = self.buf.buf_fill(src, 0)
            self.buf.init(0, chunk, len(src))
            self.buf.unlock()

            # print("ptr_len has been set: " + str(len(src)))
            self.comm.Barrier()

            if chunk < len(src):
                while True:

                    self.buf.lock()
                    self.buf.sync()

                    if self.buf.idx < self.buf.max:
                        self.buf.unlock()
                        # print(f"waiting {src_offset=}, {src_capacity=}")
                        continue

                    idx_max = self.buf.buf_fill(src, chunk)
                    self.buf.incr(0, idx_max, 0)
                    chunk += idx_max
                    self.buf.unlock()

                    # print(f"refilled buffer: {src_offset=}, {src_capacity=}, {idx_max=}")

                    if chunk >= len(src):
                        # print("done!")
                        break
        else:
            self.comm.Barrier()


    def take(self, N):

        while True:
            self.buf.lock()
            self.buf.sync()

            if self.buf.idx >= self.buf.len:
                self.buf.unlock()
                # print(f"Overrunning Src {self.buf.idx=}, {self.buf.len=}")
                return None

            if self.buf.idx >= self.buf.max:
                # print(f"{self.rank=} peeking {src_offset=} {src_capacity=} {src_len=}")
                self.buf.unlock()
                continue

            buf = self.buf.take(N)
            # print(f"{self.rank=} taking {src_offset=}, {src_capacity=}, {src_len=}")
            self.buf.unlock()

            return buf
