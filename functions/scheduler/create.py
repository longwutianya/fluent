#  Copyright 2018 U.C. Berkeley RISE Lab
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.

import logging
import random
import sys
import time
import zmq

from anna.lattices import *
from include.functions_pb2 import *
import include.server_utils as sutils
from include.shared import *
from . import utils

sys_random = random.SystemRandom()


def create_func(func_create_socket, kvs):
    func = Function()
    func.ParseFromString(func_create_socket.recv())

    name = sutils._get_func_kvs_name(func.name)
    logging.info('Creating function %s.' % (name))

    body = LWWPairLattice(generate_timestamp(0), func.body)
    kvs.put(name, body)

    funcs = utils._get_func_list(kvs, '', fullname=True)
    funcs.append(name)
    utils._put_func_list(kvs, funcs)

    func_create_socket.send(sutils.ok_resp)


def create_dag(dag_create_socket, pusher_cache, kvs, executors, dags, ip,
               pin_accept_socket, func_locations, call_frequency,
               num_replicas=1):
    serialized = dag_create_socket.recv()

    dag = Dag()
    dag.ParseFromString(serialized)
    logging.info('Creating DAG %s.' % (dag.name))

    payload = LWWPairLattice(generate_timestamp(0), serialized)
    kvs.put(dag.name, payload)

    pin_locations = {}
    for fname in dag.functions:
        candidates = set(executors)
        ip_func_map = {}
        for fn in func_locations:
            for loc in func_locations[fn]:
                if loc not in ip_func_map:
                    ip_func_map[loc] = set()
                ip_func_map[loc].add(fn)

        if sutils.ISOLATION == 'STRONG':
            for thread in ip_func_map:
                candidates.discard(thread)

        for _ in range(num_replicas):
            if len(candidates) == 0:
                sutils.error.error = NO_RESOURCES
                dag_create_socket.send(sutils.error.SerializeToString())

                # unpin any previously pinned functions because the operation
                # failed
                for loc in pin_locations:
                    _unpin_func(pin_locations[loc], loc, pusher_cache)
                return

            node, tid, = _pin_func(fname, func_locations, candidates,
                                   pin_accept_socket, ip, pusher_cache)

            if fname not in call_frequency:
                call_frequency[fname] = 0

            if fname not in func_locations:
                func_locations[fname] = set()

            func_locations[fname].add((node, tid))
            candidates.remove((node, tid))
            pin_locations[(node, tid)] = fname

    dags[dag.name] = (dag, utils._find_dag_source(dag))
    dag_create_socket.send(sutils.ok_resp)


def _pin_func(fname, ip_func_map, candidates, pin_accept_socket, ip,
              pusher_cache):
    # pick the node with the fewest functions pinned

    min_count = 1000000
    min_ip = None
    for candidate in candidates:
        if candidate in ip_func_map:
            count = len(ip_func_map[candidate])
        else:
            count = 0

        if count < min_count:
            min_count = count
            min_ip = candidate

    node, tid = min_ip

    sckt = pusher_cache.get(utils._get_pin_address(node, tid))
    msg = ip + ':' + fname
    sckt.send_string(msg)

    resp = GenericResponse()
    try:
        resp.ParseFromString(pin_accept_socket.recv())
    except zmq.ZMQError as e:
        logging.error('Pin operation to %s:%d timed out. Retrying.' %
                      (node, tid))
        # request timed out, try again
        return _pin_func(fname, ip_func_map, candidates, pin_accept_socket, ip,
                         pusher_cache)

    if resp.success:
        return node, tid
    else:  # the pin operation was rejected, remove node and try again
        logging.error('Node %s:%d rejected pin operation. Retrying.'
                      % (node, tid))

        candidates.discard((node, tid))
        return _pin_func(fname, ip_func_map, candidates, pin_accept_socket, ip,
                         pusher_cache)


def _unpin_func(fname, loc, pusher_cache):
    ip, tid = loc

    sckt = pusher_cache.get(utils._get_unpin_address(ip, tid))
    sckt.send(fname)
