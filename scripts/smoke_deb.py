#!/usr/bin/env python3
"""Exercise the installed Debian package in a disposable container only."""

import os
from pathlib import Path
import subprocess
import sys

if os.environ.get("MHM_CONTAINER_TEST") != "1" or os.geteuid() != 0:
    raise SystemExit("Run through scripts/build_linux.sh in its disposable container.")
subprocess.run(
    ["useradd", "--create-home", "--shell", "/bin/sh", "mhm-smoke"], check=True
)
program = r"""
import json, os, pathlib, struct, subprocess
subprocess.run(['gnome-keyring-daemon','--unlock','--components=secrets'], input=b'mhm-container-test\n', stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True)
def cli(*args):
    subprocess.run(['mhm-setup',*args],check=True,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
for browser in ['chrome','edge','firefox','opera']:
    identity='mhm@allowyoba.github.io' if browser=='firefox' else 'abcdefghijklmnopabcdefghijklmnop'
    cli('install','--browser',browser,'--extension-id',identity,'--no-open-browser')
cli('diagnose','--json')
proc=subprocess.Popen(['/usr/lib/mhm/native_host/native_host'],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
def request(command,**payload):
    data=json.dumps({'protocolVersion':1,'requestId':command,'command':command,**payload}).encode()
    proc.stdin.write(struct.pack('@I',len(data))+data);proc.stdin.flush()
    size=struct.unpack('@I',proc.stdout.read(4))[0]
    result=json.loads(proc.stdout.read(size))
    assert result.get('ok'),result.get('error')
    return result
assert request('ping')['security']['secure']
profile=request('saveMihomoProfile',name='Container profile',source='local',yaml='proxies:\n- {name: Local, type: direct}\n')['profile']
pid=profile['id']
assert request('profileSource',profileId=pid)['source']=='local'
request('saveProfileRules',profileId=pid,rules=['DOMAIN-SUFFIX,example.org,DIRECT'])
request('saveTrafficPolicy',policy={'enabled':True,'limitBytes':1024**3,'period':'day','cycleDay':1,'action':'block'})
assert request('connect',profileId=pid)['status']=='connected'
assert 'series' in request('trafficOverview')['traffic']
request('disconnect')
proc.stdin.close();assert proc.wait(timeout=15)==0
base=pathlib.Path.home()/'.local/share/mhm'
assert (base/'profiles.secrets.encrypted').is_file()
assert not (base/'profiles.secrets.json').exists()
assert b'Container profile' not in (base/'profiles.secrets.encrypted').read_bytes()
cli('uninstall');assert (base/'profiles.secrets.encrypted').exists()
cli('install','--browser','edge','--extension-id','abcdefghijklmnopabcdefghijklmnop','--no-open-browser')
cli('uninstall','--remove-user-data');assert not base.exists()
print('Debian PASS: four browser registrations, encrypted Secret Service storage, rules, budgets, real core, uninstall preservation and removal')
"""
subprocess.run(
    [
        "runuser",
        "-u",
        "mhm-smoke",
        "--",
        "dbus-run-session",
        "--",
        "python3",
        "-c",
        program,
    ],
    check=True,
    timeout=180,
)
subprocess.run(
    ["apt-get", "remove", "-y", "-qq", "mhm"], check=True, stdout=subprocess.DEVNULL
)
assert not Path("/usr/lib/mhm/native_host/native_host").exists()
print("APT remove PASS")
