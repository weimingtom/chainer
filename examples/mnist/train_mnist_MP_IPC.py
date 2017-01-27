#!/usr/bin/env python
from __future__ import print_function
import argparse

import chainer
import chainer.links as L

import train_mnist

import cupy
import numpy

import copy

from cupy.cuda import device
from cupy.cuda import ipc
from cupy.cuda import profiler

from multiprocessing import Pipe
from multiprocessing import Process


def main():
    # This script is almost identical to train_mnist.py. The only difference is
    # that this script uses data-parallel computation on two GPUs.
    # See train_mnist.py for more details.
    parser = argparse.ArgumentParser(description='Chainer example: MNIST')
    parser.add_argument('--batchsize', '-b', type=int, default=400,
                        help='Number of images in each mini-batch')
    parser.add_argument('--epoch', '-e', type=int, default=20,
                        help='Number of sweeps over the dataset to train')
    parser.add_argument('--gpu0', '-g', type=int, default=0,
                        help='First GPU ID')
    parser.add_argument('--gpu1', '-G', type=int, default=1,
                        help='Second GPU ID')
    parser.add_argument('--out', '-o', default='result_parallel',
                        help='Directory to output the result')
    parser.add_argument('--resume', '-r', default='',
                        help='Resume the training from snapshot')
    parser.add_argument('--unit', '-u', type=int, default=1000,
                        help='Number of units')
    args = parser.parse_args()

    print('GPU: {}, {}'.format(args.gpu0, args.gpu1))
    print('# unit: {}'.format(args.unit))
    print('# Minibatch-size: {}'.format(args.batchsize))
    print('# epoch: {}'.format(args.epoch))
    print('')

    model = L.Classifier(train_mnist.MLP(args.unit, 10))

    train, test = chainer.datasets.get_mnist()

    epoch = args.epoch
    batchsize = args.batchsize
    datasize = len(train)
    proc_num = 2

    conns = []
    procs = []

    # start slave process(es)
    for i in range(1, proc_num):
        conn, c_conn = Pipe()
        proc_id = i
        gpu_id = i
        proc = Process(target=run_training,
                       args=((c_conn,), proc_id, proc_num, gpu_id, model,
                             epoch, datasize, batchsize, train, test))
        proc.start()
        conns.append(conn)
        procs.append(proc)

    # start master process
    proc_id = 0
    gpu_id = 0
    run_training(conns, proc_id, proc_num, gpu_id, model,
                 epoch, datasize, batchsize, train, test)

    for proc in procs:
        proc.join()


def run_training(conns, proc_id, proc_num, gpuid, model_ref,
                 epoch, datasize, batchsize, train, test):

    with device.Device(gpuid):
        my_dev = chainer.cuda.get_device(gpuid)

        if proc_id == 0:
            model = model_ref
        else:
            model = copy.deepcopy(model_ref)
        model.to_gpu()

        optimizer = chainer.optimizers.Adam()
        optimizer.setup(model)

        for epoch in range(epoch):

            if proc_id != 0:
                # slave process(es)
                indexes = conns[0].recv()
            else:
                # master process
                print('epoch %d' % epoch)
                indexes = numpy.random.permutation(datasize)
                for conn in conns:
                    conn.send(indexes)

            for i in range(0, datasize, batchsize):

                x_batch = train[indexes[i:i+batchsize]][0]
                y_batch = train[indexes[i:i+batchsize]][1]

                x = chainer.Variable(x_batch[proc_id::proc_num])
                y = chainer.Variable(y_batch[proc_id::proc_num])
                x.to_gpu()
                y.to_gpu()

                loss = model(x, y)

                model.cleargrads()
                loss.backward()

                gg = model.gather_grads()
                dev_gg = chainer.cuda.get_device(gg)
                mh = ipc.IpcMemoryHandle(gg)

                if proc_id != 0:
                    # slave process(es)

                    # send memory handle to master
                    conns[0].send(mh)

                    # recv memory handle from master and open it
                    mhm = conns[0].recv()
                    ggm = mhm.open()

                    # copy ggm (master's gg) to gg (gg = ggm)
                    cupy.copyto(gg, ggm)

                    # send "ok" message to master
                    my_dev.synchronize()
                    conns[0].send("ok")

                else:
                    # master process

                    for conn in conns:
                        # recv memory handle from slave and open it
                        mhs = conn.recv()
                        ggs = mhs.open()

                        # add ggs (slave's gg) to gg (master's gg)
                        dev_ggs = chainer.cuda.get_device(ggs)
                        if dev_gg == dev_ggs:
                            gg += ggs
                        else:
                            ggs_copy = chainer.cuda.to_gpu(ggs, device=dev_gg)
                            gg += ggs_copy

                    my_dev.synchronize()
                    for conn in conns:
                        # send memory handle to slave
                        conn.send(mh)

                    for conn in conns:
                        # wait for "ok" message from slave
                        conn.recv()

                model.scatter_grads(gg)

                optimizer.update()

        profiler.stop()


if __name__ == '__main__':
    main()