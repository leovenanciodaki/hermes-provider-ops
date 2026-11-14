"""Lossless Docker v1.45 recreation recipe; no daemon access on import."""
import copy
import http.client
import json
import socket
from urllib.parse import quote

RETIRED = frozenset()  # Existing unrelated overlays are part of the runtime contract.
class Connection(http.client.HTTPConnection):
    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX)
        self.sock.settimeout(35)
        self.sock.connect('/var/run/docker.sock')

def api(method, path, payload=None):
    c = Connection('localhost', timeout=35)
    try:
        c.request(method, '/v1.45'+path, json.dumps(payload) if payload is not None else None,
                  {'Content-Type':'application/json'})
        r=c.getresponse(); data=r.read()
        if r.status >= 300: raise RuntimeError('Docker HTTP '+str(r.status))
        return json.loads(data) if data else {}
    finally: c.close()

def recipe(inspect, image):
    payload=copy.deepcopy(inspect['Config'])
    payload['Image']=image
    payload['HostConfig']=copy.deepcopy(inspect['HostConfig'])
    # HostConfig already contains bind/volume options, propagation and resource limits.
    endpoints={}
    for name, endpoint in inspect['NetworkSettings']['Networks'].items():
        endpoints[name]={k:copy.deepcopy(endpoint[k]) for k in
            ('IPAMConfig','Links','Aliases','DriverOpts','GwPriority') if endpoint.get(k) is not None}
    payload['NetworkingConfig']={'EndpointsConfig':endpoints}
    return payload
