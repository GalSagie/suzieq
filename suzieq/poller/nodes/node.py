import sys
from collections import defaultdict
import os
import time
from datetime import datetime
import logging
import random
from http import HTTPStatus
import json

import yaml
from urllib.parse import urlparse

import asyncio
import asyncssh
import aiohttp
from asyncio.subprocess import PIPE
from concurrent.futures._base import TimeoutError

from suzieq.poller.services.service import RsltToken
from suzieq.poller.genhosts import process_ansible_inventory

logger = logging.getLogger(__name__)


def get_hostsdata_from_hostsfile(hosts_file) -> dict:
    """Read the suzieq devices file and return the data from the file"""

    if not os.path.isfile(hosts_file):
        logger.error("hosts config must be a file")
        print("hosts config must be a file")
        sys.exit(1)

    if not os.access(hosts_file, os.R_OK):
        logger.error("hosts config file is not readable: {}", hosts_file)
        print("hosts config file is not readable: {}", hosts_file)
        sys.exit(1)

    with open(hosts_file, "r") as f:
        try:
            hostsconf = yaml.safe_load(f.read())
        except Exception as e:
            logger.error("Invalid hosts config file:{}", e)
            print("Invalid hosts config file:{}", e)
            sys.exit(1)

    return hostsconf


async def init_hosts(hosts_file, ansible_file, namespace):
    """Process list of devices to gather data from.
    This involves creating a node for each device listed, and connecting to
    those devices and initializing state about those devices"""

    nodes = {}

    if hosts_file:
        hostsconf = get_hostsdata_from_hostsfile(hosts_file)
    else:
        hostlines = process_ansible_inventory(ansible_file, namespace)
        hostsconf = yaml.safe_load('\n'.join(hostlines))

    if not hostsconf:
        print("ERROR: No hosts specified via hosts or ansible inventory file")
        sys.exit(1)

    for namespace in hostsconf:
        if "namespace" not in namespace:
            logger.warning('No namespace specified, assuming "default"')
            nsname = "default"
        else:
            nsname = namespace["namespace"]

        tasks = []
        for host in namespace.get("hosts", None):
            entry = host.get("url", None)
            if entry:
                words = entry.split()
                result = urlparse(words[0])

                username = result.username
                password = result.password or "vagrant"
                port = result.port
                host = result.hostname
                devtype = None
                keyfile = None

                for i in range(1, len(words[1:])+1):
                    if words[i].startswith('keyfile'):
                        keyfile = words[i].split("=")[1]
                    elif words[i].startswith('devtype'):
                        devtype = words[i].split("=")[1]

                newnode = Node()
                tasks += [newnode._init(
                    address=host,
                    username=username,
                    port=port,
                    password=password,
                    transport=result.scheme,
                    devtype=devtype,
                    ssh_keyfile=keyfile,
                    namespace=nsname,
                )]

        for f in asyncio.as_completed(tasks):
            newnode = await f
            if newnode.devtype is None:
                logger.error(
                    "Unable to determine device type for {}".format(host))
            else:
                logger.info(f"Added node {newnode.hostname}")

            nodes.update(
                {"{}.{}".format(nsname, newnode.hostname): newnode})

    return nodes


class Node(object):
    @property
    def status(self):
        return self._status

    @property
    def last_exception(self) -> Exception:
        return self._last_exception

    @last_exception.setter
    def last_exception(self, val: Exception):
        self._last_exception = val
        self._last_exception_timestamp = int(time.time()*1000)

    async def _init(self, **kwargs):
        if not kwargs:
            raise ValueError

        self.hostname = "-"  # Device hostname
        self.devtype = None  # Device type
        self.pvtkey_file = ""     # SSH private keyfile
        self.prev_result = {}  # No updates if nothing changed
        self.nsname = None
        self.svc_cmd_mapping = defaultdict(lambda: {})  # Not used yet
        self.logger = logging.getLogger(__name__)
        self.port = 0
        self.backoff = 15  # secs to backoff
        self.init_again_at = 0  # after this epoch secs, try init again
        self.connect_timeout = 10  # connect timeout in seconds
        self.cmd_timeout = 10  # default command timeout in seconds
        self.batch_size = 4    # Number of commands to issue in parallel
        self.bootupTimestamp = 0
        self.version = 0                 # OS Version to pick the right defn
        self._service_queue = None
        self._conn = None
        self._status = "init"
        self.svcs_proc = set()
        self.error_svcs_proc = set()
        self.ssh_ready = asyncio.Event()
        self._last_exception = None
        self._last_exception_timestamp = None

        self.address = kwargs["address"]
        self.hostname = kwargs["address"]  # default till we get hostname
        self.username = kwargs.get("username", "vagrant")
        self.password = kwargs.get("password", "vagrant")
        self.transport = kwargs.get("transport", "ssh")
        self.nsname = kwargs.get("namespace", "default")
        self.port = kwargs.get("port", 0)
        self.devtype = None
        pvtkey_file = kwargs.get("ssh_keyfile", None)
        if pvtkey_file:
            try:
                self.pvtkey = asyncssh.public_key.read_private_key(pvtkey_file)
            except Exception as e:
                self.logger.error("ERROR: Unable to read private key file {} "
                                  "for {} due to {}".format(pvtkey_file,
                                                            self.address,
                                                            str(e)))
                self.logger.error("ERROR: Falling back to password for {}"
                                  .format(self.address))
                self.pvtkey = None
        else:
            self.pvtkey = None

        self._init_service_queue()

        self.ssh_ready.set()
        if not self.port:
            if self.transport == "ssh":
                self.port = 22
                await self._init_ssh(init_boot_time=False)
            elif self.transport == "https":
                self.port = 443

        devtype = kwargs.get("devtype", None)
        if devtype:
            self.set_devtype(devtype)

        await self.init_node()
        if not self.hostname:
            self.hostname = self.address

        if self._status == "init":
            self.backoff = min(600, self.backoff * 2) + \
                (random.randint(0, 1000) / 1000)
            self.init_again_at = time.time() + self.backoff
        return self

    async def init_node(self):
        devtype = None
        hostname = None

        if self.transport == "ssh" or self.transport == "local":
            try:
                await self.get_device_type_hostname()
                devtype = self.devtype
            except Exception as e:
                self.last_exception = e
                devtype = None

            if not devtype:
                self.logger.debug(
                    f"no devtype for {self.hostname} {self.last_exception}")
                self._status = "init"
                return
            else:
                self._status = "good"
                self.set_devtype(devtype)

                if hostname:
                    self.set_hostname(hostname)

            await self.init_boot_time()

    def set_devtype(self, devtype):
        self.devtype = devtype

        if self.devtype == "cumulus":
            self.__class__ = CumulusNode
        elif self.devtype == "eos":
            self.__class__ = EosNode
        elif any(n == self.devtype for n in ["Ubuntu", "Debian", "Red Hat", "Linux"]):
            self.__class__ = LinuxNode
            self.devtype = "linux"

    async def get_device_type_hostname(self):
        """Determine the type of device we are talking to if using ssh/local"""
        # There isn't that much of a difference in running two commands versus
        # running them one after the other as this involves an additional ssh
        # setup time. show version works on most networking boxes and
        # hostnamectl on Linux systems. That's all we support today.
        await self.exec_cmd(self._parse_device_type_hostname,
                            ["show version", "hostnamectl",
                             "cat /etc/os-release", "show hostname"], None)

    async def _parse_device_type_hostname(self, output, cb_token) -> None:
        devtype = ""
        hostname = None

        if output[0]["status"] == 0:
            data = output[1]["data"]
            if "Arista " in data:
                devtype = "eos"
            elif "JUNOS " in data:
                devtype = "junos"
            elif "NX-OS" in data:
                devtype = "nxos"

            if output[4]["status"] == 0:
                hostname = output[4]["data"].strip()

        elif output[1]["status"] == 0:
            data = output[1]["data"]
            if "Cumulus Linux" in data:
                devtype = "cumulus"
            elif "Ubuntu" in data:
                devtype = "Ubuntu"
            elif "Red Hat" in data:
                devtype = "RedHat"
            elif "Debian GNU/Linux" in data:
                devtype = "Debian"
            else:
                devtype = "linux"

            # Hostname is in the first line of hostnamectl
            hostline = data.splitlines()[0].strip()
            if hostline.startswith("Static hostname"):
                _, hostname = hostline.split(":")
                hostname = hostname.strip()

            if output[2]["status"] == 0:
                data = output[2]["data"]
                for line in data.splitlines():
                    if line.startswith("VERSION_ID"):
                        self.version = line.split('=')[1] \
                                           .strip().replace('"', '')
                        break

        self.devtype = devtype
        self.set_hostname(hostname)

    async def _parse_boottime_hostname(self, output, cb_token) -> None:
        """Parse the uptime command output"""

        if output[0]["status"] == 0:
            upsecs = output[0]["data"].split()[0]
            self.bootupTimestamp = int(int(time.time()*1000)
                                       - float(upsecs)*1000)
        if output[1]["status"] == 0:
            self.hostname = output[1]["data"].strip()

    def set_unreach_status(self):
        self._status = "unreachable"

    def set_good_status(self):
        self._status = "good"

    def is_alive(self):
        return self._status == "good"

    def set_hostname(self, hostname=None):
        if hostname:
            self.hostname = hostname

    async def local_gather(self, service_callback, cmd_list, cb_token) -> None:
        """Given a dictionary of commands, run locally and return outputs"""

        result = []
        for cmd in cmd_list:
            proc = await asyncio.create_subprocess_shell(cmd, stdout=PIPE,
                                                         stderr=PIPE)

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(), timeout=self.cmd_timeout)

                if not proc.returncode:
                    d = stdout.decode('ascii', 'ignore')
                    result.append(self._create_result(cmd))
                else:
                    d = stderr('ascii', 'ignore')
                    result.append(self._create_error(cmd))

            except asyncio.TimeoutError as e:
                self.last_exception = e
                result.append(self._create_error(cmd))

        await service_callback(result, cb_token)

    async def init_boot_time(self):
        """Fill in the boot time of the node by running the appropriate command"""
        await self.exec_cmd(self._parse_boottime_hostname, ["cat /proc/uptime",
                                                            "hostname"], None)

    def post_commands(self, service_callback, svc_defn: dict,
                      cb_token: RsltToken):
        if cb_token:
            cb_token.nodeQsize = self._service_queue.qsize()
        self._service_queue.put_nowait([service_callback, svc_defn, cb_token])

    async def run(self):
        tasks = []
        while True:
            while (len(tasks) < self.batch_size):
                request = await self._service_queue.get()
                if request:
                    tasks.append(self.exec_service(
                        request[0], request[1], request[2]))
                    self.logger.debug(
                        f"Scheduling {request[2].service} for execution")
                if self._service_queue.empty():
                    break
            if tasks:
                done, pending = await asyncio.wait(
                    tasks, return_when=asyncio.FIRST_COMPLETED)

                tasks = list(pending)

    async def ssh_gather(self, service_callback, cmd_list, cb_token, timeout):
        """Given a dictionary of commands, run ssh and place output on service callback"""

        result = []

        if cmd_list is None:
            await service_callback({}, cb_token)

        if not self._conn:
            await self._init_ssh()
            if not self._conn:
                for cmd in cmd_list:
                    self.logger.error(
                        "Unable to connect to node {} cmd {}".format(
                            self.hostname, cmd))
                    result.append(self._create_error(cmd))
                await service_callback(result, cb_token)
                return

        if isinstance(cb_token, RsltToken):
            cb_token.node_token = self.bootupTimestamp

        timeout = timeout or self.cmd_timeout
        for cmd in cmd_list:
            try:
                output = await asyncio.wait_for(self._conn.run(cmd),
                                                timeout=timeout)
                result.append(self._create_result(
                    cmd, output.exit_status, output.stdout))
            except Exception as e:
                self.last_exception = e
                result.append(self._create_error(cmd))
                self.logger.error(
                    "Unable to connect to node {} cmd {}".format(
                        self.hostname, cmd))
                self._conn = None
                break

        await service_callback(result, cb_token)

    async def _close_connection(self):
        self._conn.close()
        await self._conn.wait_closed()
        self._conn = None

    def _init_service_queue(self):
        if not self._service_queue:
            self._service_queue = asyncio.Queue()

    async def _init_ssh(self, init_boot_time=True) -> None:
        await self.ssh_ready.wait()
        if not self._conn:
            self.ssh_ready.clear()
            try:
                self._conn = await asyncio.wait_for(
                    asyncssh.connect(
                        self.address,
                        port=self.port,
                        known_hosts=None,
                        client_keys=self.pvtkey if self.pvtkey else None,
                        username=self.username,
                        password=self.password if not self.pvtkey else None,
                    ),
                    timeout=self.cmd_timeout,
                )
                self.logger.info(
                    f"Connected to {self.address} at {time.time()}")
                self.ssh_ready.set()
                if init_boot_time:
                    await self.init_boot_time()
            except Exception as e:
                self.logger.error(f"ERROR: Unable to connect, {str(e)}")
                self.last_exception = e
                self._conn = None
                self.ssh_ready.set()
        return

    def _create_error(self, cmd) -> dict:
        data = {'error': str(self.last_exception)}
        if isinstance(self.last_exception, TimeoutError):
            status = HTTPStatus.REQUEST_TIMEOUT
        elif isinstance(self.last_exception, asyncssh.misc.ProtocolError):
            status = HTTPStatus.FORBIDDEN
        elif hasattr(self.last_exception, 'code'):
            status = self.last_exception.code
        else:
            status = -1
        return self._create_result(cmd, status, data)

    def _create_result(self, cmd, status, data) -> dict:
        result = {
            "status": status,
            "timestamp": int(datetime.utcnow().timestamp() * 1000),
            "cmd": cmd,
            "devtype": self.devtype,
            "namespace": self.nsname,
            "hostname": self.hostname,
            "address": self.address,
            "version": self.version,
            "data": data,
        }
        return result

    async def rest_gather(self, svc_dict, oformat="json"):
        raise NotImplementedError

    async def exec_cmd(self, service_callback, cmd_list, cb_token,
                       oformat='json', timeout=None):

        if self.transport == "ssh":
            await self.ssh_gather(service_callback, cmd_list, cb_token, timeout)
        elif self.transport == "https":
            await self.rest_gather(service_callback, cmd_list,
                                   cb_token, oformat, timeout)
        elif self.transport == "local":
            await self.local_gather(service_callback, cmd_list,
                                    cb_token, timeout)
        else:
            self.logger.error(
                "Unsupported transport {} for node {}".format(
                    self.transport, self.hostname
                )
            )

        return

    async def exec_service(self, service_callback, svc_defn: dict,
                           cb_token: RsltToken):

        result = []  # same type as gather function
        cmd = None
        if not svc_defn:
            return result

        if self._status == "init":
            if self.init_again_at < time.time():
                await self.init_node()

        if self._status == "init":
            result.append(self._create_error(svc_defn.get("service", "-")))
            return await service_callback(result, cb_token)

        # Update our boot time value into the callback token
        if cb_token:
            cb_token.bootupTimestamp = self.bootupTimestamp

        self.svcs_proc.add(svc_defn.get("service"))
        use = svc_defn.get(self.hostname, None)
        if not use:
            use = svc_defn.get(self.devtype, {})
        if not use:
            if svc_defn.get("service") not in self.error_svcs_proc:
                result.append(self._create_result(
                    svc_defn, HTTPStatus.NOT_FOUND, "No service definition"))
                self.error_svcs_proc.add(svc_defn.get("service"))
            return await service_callback(result, cb_token)

        # TODO This kind of logic should be encoded in config and node shouldn't have to know about it
        if "copy" in use:
            use = svc_defn.get(use.get("copy"))

        if use:
            if isinstance(use, list):
                # There's more than one version here, we have to pick ours
                for item in use:
                    if (item["version"] == "all" or
                            item["version"] == self.version):
                        cmd = item.get("command", None)
                        break
            else:
                cmd = use.get("command", None)

        oformat = svc_defn.get(self.devtype, {}).get("format", "json")

        if not cmd:
            return result

        if type(cmd) is not list:
            cmd = [cmd]

        await self.exec_cmd(service_callback, cmd, cb_token,
                            oformat=oformat, timeout=cb_token.timeout)


class EosNode(Node):
    def _init(self, **kwargs):
        super()._init(kwargs)
        self.devtype = "eos"

    async def init_node(self):
        try:
            await self.get_device_boottime_hostname()
        except Exception as e:
            self.last_exception = e

    async def get_device_type_hostname(self):
        raise NotImplementedError

    async def get_device_boottime_hostname(self):
        """Determine the type of device we are talking to"""

        if self.transport == 'https':
            cmdlist = ["show version", "show hostname"]
        else:
            cmdlist = ["show version|json", "show hostname|json"]
        await self.exec_cmd(self._parse_boottime_hostname, cmdlist, None)

    async def _parse_boottime_hostname(self, output, cb_token) -> None:

        if output[0]["status"] == 0 or output[0]["status"] == 200:
            data = output[0]["data"]
            self.bootupTimestamp = data["bootupTimestamp"]

        if output[1]["status"] == 0 or output[1]["status"] == 200:
            data = output[1]["data"]
            self.hostname = data["hostname"]
            self._status = "good"

    async def rest_gather(self, service_callback, cmd_list, cb_token,
                          oformat="json", timeout=None):

        result = []
        if not cmd_list:
            return result

        timeout = timeout or self.cmd_timeout

        now = int(datetime.utcnow().timestamp() * 1000)
        auth = aiohttp.BasicAuth(self.username, password=self.password)
        data = {
            "jsonrpc": "2.0",
            "method": "runCmds",
            "id": int(now),
            "params": {"version": 1, "format": oformat, "cmds": cmd_list},
        }
        headers = {"Content-Type": "application/json"}
        if self.port:
            url = "https://{}:{}/command-api".format(self.address, self.port)
        else:
            url = "https://{}:{}/command-api".format(self.address, self.port)

        output = []
        status = 200  # status OK

        try:
            async with aiohttp.ClientSession(
                    auth=auth,
                    conn_timeout=self.connect_timeout,
                    read_timeout=timeout,
                    connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                async with session.post(url, json=data, headers=headers) as response:
                    status, json_out = response.status, await response.json()
                    if "result" in json_out:
                        output.extend(json_out["result"])
                    else:
                        output.extend(json_out["error"])

            for i, cmd in enumerate(cmd_list):
                result.append(
                    {
                        "status": status,
                        "timestamp": now,
                        "cmd": cmd,
                        "devtype": self.devtype,
                        "namespace": self.nsname,
                        "hostname": self.hostname,
                        "address": self.address,
                        "data": output[i] if type(output) is list else output,
                    }
                )
        except Exception as e:
            self.last_exception = e
            for cmd in cmd_list:
                result.append(self._create_error(cmd))
            self._status = "init"  # Recheck everything
            self.logger.error("ERROR: (REST) Unable to communicate with node "
                              "{} due to {}".format(self.address, str(e)))

        await service_callback(result, cb_token)

    async def _parse_hostname(self, output, cb_token) -> None:
        """Parse the hostname command output"""
        if not output:
            self.hostname = "-"
            return

        if output[0]["status"] == 0:
            data = output[1]["data"]
            try:
                jout = json.loads(data)
                self.hostname = jout["hostname"]
            except:
                self.hostname = "-"


class CumulusNode(Node):
    def _init(self, **kwargs):
        if "username" not in kwargs:
            kwargs["username"] = "cumulus"
        if "password" not in kwargs:
            kwargs["password"] = "CumulusLinux!"

        super()._init(kwargs)
        self.devtype = "cumulus"

    async def rest_gather(self, service_callback, cmd_list, cb_token,
                          oformat='json', timeout=None):

        result = []
        if not cmd_list:
            return result

        auth = aiohttp.BasicAuth(self.username, password=self.password)
        url = "https://{0}:{1}/nclu/v1/rpc".format(self.address, self.port)
        headers = {"Content-Type": "application/json"}

        try:
            async with aiohttp.ClientSession(
                    auth=auth,
                    timeout=timeout or self.cmd_timeout,
                    connector=aiohttp.TCPConnector(ssl=False),
            ) as session:
                for cmd in cmd_list:
                    data = {"cmd": cmd}
                    async with session.post(
                            url, json=data, headers=headers
                    ) as response:
                        result.append(
                            {
                                "status": response.status,
                                "timestamp": int(datetime.utcnow().timestamp() * 1000),
                                "cmd": cmd,
                                "devtype": self.devtype,
                                "namespace": self.nsname,
                                "hostname": self.hostname,
                                "address": self.address,
                                "data": await response.text(),
                            }
                        )
        except Exception as e:
            self.last_exception = e
            result.append(self._create_error(cmd))
            self.logger.error("ERROR: (REST) Unable to communicate with node "
                              "{} due to {}".format(self.address, str(e)))

        await service_callback(result, cb_token)


class LinuxNode(Node):
    def _init(self, **kwargs):
        super()._init(kwargs)
        self.devtype = "linux"
