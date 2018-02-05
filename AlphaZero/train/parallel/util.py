import multiprocessing as mp
import queue
import threading as thrd
from flask import Flask, request
import requests


def printlog(*msg):
    print('[' + mp.current_process().name + ']', *msg)


def printlog_thrd(*msg):
    print('[' + thrd.current_thread().name + ']', *msg)


class RWLock:
    def __init__(self):
        self.w_pending = mp.Lock()
        self.r_count = mp.Value('i', 0)
        self.r_lock = mp.Lock()

    def r_acquire(self):
        self.w_pending.acquire()
        self.r_count.value += 1
        if self.r_count.value == 1:
            self.r_lock.acquire()
        self.w_pending.release()

    def r_release(self):
        self.r_count.value -= 1
        if self.r_count.value == 0:
            self.r_lock.release()

    def w_acquire(self):
        self.w_pending.acquire()
        self.r_lock.acquire()

    def w_release(self):
        self.r_lock.release()
        self.w_pending.release()


class Reception:
    def __init__(self, conn_num):
        self.queue = mp.Queue(conn_num)
        self.conn_num = conn_num
        self.conn_idx_queue = mp.Queue(self.conn_num)
        self.conns = []
        for i in range(self.conn_num):
            self.conns.append(mp.Pipe())
            self.conn_idx_queue.put(i)

    def req(self, r):
        i = self.conn_idx_queue.get()
        r_conn, s_conn = self.conns[i]
        self.queue.put((r, s_conn))
        res = r_conn.recv()
        self.conn_idx_queue.put(i)
        return res

    def get(self, block=True):
        r, s_conn = self.queue.get(block)
        return r, s_conn


class Reception_Thrd:
    def __init__(self, conn_num):
        self.queue = queue.Queue(conn_num)
        self.conn_num = conn_num
        self.conn_idx_queue = queue.Queue(self.conn_num)
        self.conns = []
        for i in range(self.conn_num):
            self.conns.append(queue.Queue(1))
            self.conn_idx_queue.put(i)

    def req(self, r):
        i = self.conn_idx_queue.get()
        conn = self.conns[i]
        self.queue.put((r, conn))
        res = conn.get()
        self.conn_idx_queue.put(i)
        return res

    def get(self, block=True):
        r, conn = self.queue.get(block)
        return r, conn

def Block_Pipe():
    bc = Block_Conn()
    return bc, bc
class Block_Conn:
    def __init__(self):
        self.data_r, self.data_s = mp.Pipe()
        self.ack_r, self.ack_s = mp.Pipe()

    def send(self, msg):
        self.data_s.send(msg)
        self.ack_r.recv()

    def recv(self):
        msg = self.data_r.recv()
        self.ack_s.send(True)
        return msg

def HTTP_Pipe():
    pass
class HTTP_recv:
    def __init__(self):
        pass

    def recv(self):
        pass
class HTTP_send:
    def __init__(self):
        pass

    def send(self):
        pass
