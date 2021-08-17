#!/usr/bin/env python
# -*- coding: utf-8 -*-


import numpy    as     np
from   mpi4py   import MPI

from   producer import Producer


comm = MPI.COMM_WORLD
rank = comm.Get_rank()

buff_size = 10
data_size = 30

producer = Producer(buff_size)

data = np.random.rand(data_size)
producer.fill(data)

print(f"{rank=} {data=}")

res = 0
for i in range(data_size):
    p = producer.take(1)
    if p is not None:
        print(f"{rank=}, {i=}, {p=}")
        res += 1

print(f"{res=}")
