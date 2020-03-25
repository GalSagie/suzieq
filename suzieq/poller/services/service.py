import os
import asyncio
import random
from datetime import datetime
import time
import copy
import logging
import json
import yaml
import sys
from tempfile import mkstemp

import pyarrow as pa

from suzieq.poller.services.svcparser import cons_recs_from_json_template

HOLD_TIME_IN_MSECS = 60000  # How long b4 declaring node dead


class Service(object):

    def __init__(self, name, defn, period, stype, keys, ignore_fields, schema,
                 write_queue, run_once="forever"):
        self.name = name
        self.defn = defn
        self.ignore_fields = ignore_fields or []
        self.write_queue = write_queue
        self.keys = keys
        self.schema = schema
        self.period = period
        self.stype = stype
        self.logger = logging.getLogger("suzieq")
        self.run_once = run_once

        self.update_nodes = False  # we have a new node list
        self.rebuild_nodelist = False  # used only when a node gets init
        self.nodes = {}
        self.new_nodes = {}
        self.post_commands = []
        self._node_queue = None
        self.previous_results = {}

        # Add the hidden fields to ignore_fields
        self.ignore_fields.append("timestamp")

        if "namespace" not in self.keys:
            self.keys.insert(0, "namespace")

        if "hostname" not in self.keys:
            self.keys.insert(1, "hostname")

        if self.stype == "counters":
            self.partition_cols = ["namespace", "hostname"]
        else:
            self.partition_cols = self.keys + ["timestamp"]

        self._init_node_queue()

    def get_data(self):
        """provide the data that is interesting for a service

        does not include write_queue or schema"""

        r = {}
        for field in 'name defn ignore_fields keys period stype partition_cols'.split(' '):
            r[field] = getattr(self, field)
        # textFSM objects can't be jsoned, so changing it
        for device in r['defn']:
            if 'textfsm' in r['defn'][device]:
                r['defn'][device]['textfsm'] = str(
                    r['defn'][device]['textfsm'])
        return r

    def set_nodes(self, nodes):
        """New node list for this service"""
        # TODO: does this need to do something different if the nodelist changes?
        self.nodes = nodes
        for node in nodes.values():
            self.previous_results[node.hostname] = {}
            self.post_commands.append(node.post_commands)

    def get_empty_record(self):
        map_defaults = {
            pa.string(): "",
            pa.int32(): 0,
            pa.int64(): 0,
            pa.float32(): 0.0,
            pa.float64(): 0.0,
            pa.date64(): 0.0,
            pa.bool_(): False,
            pa.list_(pa.string()): [],
            pa.list_(pa.int64()): [],
        }

        defaults = [map_defaults[x] for x in self.schema.types]
        rec = dict(zip(self.schema.names, defaults))

        return rec

    def _dump_output(self, outputs) -> str:
        """Dump the output to a YAML file"""
        if outputs:
            yml = {"service": self.name,
                   "type": self.run_once,
                   "output": outputs}
            fd, name = mkstemp(suffix=f"-{self.name}-poller.yml")
            f = os.fdopen(fd, "w")
            f.write(yaml.dump(yml))
            f.close()
            return name
        return ""

    def get_diff(self, old, new):
        """Compare list of dictionaries ignoring certain fields
        Return list of adds and deletes
        """
        adds = []
        dels = []
        koldvals = {}
        knewvals = {}
        koldkeys = []
        knewkeys = []

        for i, elem in enumerate(old):
            vals = [v for k, v in elem.items() if k not in self.ignore_fields]
            kvals = [v for k, v in elem.items() if k in self.keys]
            koldvals.update({tuple(str(vals)): i})
            koldkeys.append(kvals)

        for i, elem in enumerate(new):
            vals = [v for k, v in elem.items() if k not in self.ignore_fields]
            kvals = [v for k, v in elem.items() if k in self.keys]
            knewvals.update({tuple(str(vals)): i})
            knewkeys.append(kvals)

        addlist = [v for k, v in knewvals.items() if k not in koldvals.keys()]
        dellist = [v for k, v in koldvals.items() if k not in knewvals.keys()]

        adds = [new[v] for v in addlist]
        dels = [old[v] for v in dellist if koldkeys[v] not in knewkeys]

        if adds and self.stype == "counters":
            # If there's a change in any field of the counters, update them all
            # simplifies querying
            adds = new

        return adds, dels

    def textfsm_data(self, raw_input, fsm_template, schema, data):
        """Convert unstructured output to structured output"""

        records = []
        fsm_template.Reset()
        res = fsm_template.ParseText(raw_input)

        for entry in res:
            metent = dict(zip(fsm_template.header, entry))
            records.append(metent)

        result = self.clean_data(records, data)

        fields = [fld.name for fld in schema]

        ptype_map = {
            pa.string(): str,
            pa.int32(): int,
            pa.int64(): int,
            pa.float32(): float,
            pa.float64(): float,
            pa.date64(): float,
            pa.list_(pa.string()): list,
            pa.list_(pa.int64()): list,
            pa.bool_(): bool,
            pa.list_(pa.struct([('nexthop', pa.string()),
                                ('oif', pa.string()),
                                ('weight', pa.int32())])): list,
        }

        map_defaults = {
            pa.string(): "",
            pa.int32(): 0,
            pa.int64(): 0,
            pa.float32(): 0.0,
            pa.float64(): 0.0,
            pa.date64(): 0.0,
            pa.bool_(): False,
            pa.list_(pa.string()): [],
            pa.list_(pa.int64()): [],
            pa.list_(pa.struct([('nexthop', pa.string()),
                                ('oif', pa.string()),
                                ('weight', pa.int32())])): [("", "", 1)]
        }

        # Ensure the type is set correctly.
        for entry in result:
            for cent in entry:
                if cent in fields:
                    schent_type = schema.field(cent).type
                    if not isinstance(entry[cent], ptype_map[schent_type]):
                        if entry[cent]:
                            entry[cent] = ptype_map[schent_type](entry[cent])
                        else:
                            entry[cent] = map_defaults[schent_type]
                    elif isinstance(entry[cent], list):
                        for i, ele in enumerate(entry[cent]):
                            if not isinstance(ele, ptype_map[schent_type.value_type]):
                                try:
                                    if ptype_map[schent_type.value_type] == int:
                                        entry[cent][i] = int(entry[cent][i])
                                    else:
                                        raise ValueError
                                except ValueError:
                                    entry[cent][i] = (
                                        map_defaults[schent_type.value_type])

        return result

    def _init_node_queue(self):
        if not self._node_queue:
            self._node_queue = asyncio.Queue()

    async def post_results(self, results):
        await self._node_queue.put(results)

    async def schedule_commands(self):
        """ these are the commands that we want to be run on nodes """
        random.shuffle(self.post_commands)
        [await command(self.post_results, self.defn) for command in self.post_commands]

    async def gather_data(self):
        """Collect data invoking the appropriate get routine from nodes."""
        outputs = []
        now = time.time()
        count = self._node_queue.qsize()

        while count > 0:
            outputs.append(await self._node_queue.get())
            count -= 1

        if len(outputs) > 0:
            self.logger.info(f"gathered {len(outputs)} for {self.name}")
        return outputs

    def process_data(self, data):
        """Derive the data to be stored from the raw input"""
        result = []

        if (data["status"] is not None) and (int(data["status"]) == 200 or
                                             int(data["status"]) == 0):
            if not data["data"]:
                return result

            nfn = self.defn.get(data.get("hostname"), None)
            if not nfn:
                nfn = self.defn.get(data.get("devtype"), None)
            if nfn:
                copynfn = nfn.get("copy", None)
                if copynfn:
                    nfn = self.defn.get(copynfn, {})
                if nfn.get("normalize", None):
                    if isinstance(data["data"], str):
                        try:
                            in_info = json.loads(data["data"])
                        except json.JSONDecodeError:
                            self.logger.error(
                                "Received non-JSON output where "
                                "JSON was expected for {} on "
                                "node {}, {}".format(
                                    data["cmd"], data["hostname"], data["data"]
                                )
                            )
                            return result
                    else:
                        in_info = data["data"]

                    if in_info:
                        # EOS' HTTP returns errors like this.
                        tmp = in_info.get('data', [])
                        if tmp and tmp[0].get('errors', {}):
                            return []

                        result = cons_recs_from_json_template(
                            nfn.get("normalize", ""), in_info)

                        result = self.clean_data(result, data)
                else:
                    tfsm_template = nfn.get("textfsm", None)
                    if not tfsm_template:
                        return result

                    if "output" in data["data"]:
                        in_info = data["data"]["output"]
                    elif "messages" in data["data"]:
                        # This is Arista's bash output format
                        in_info = data["data"]["messages"][0]
                    else:
                        in_info = data["data"]
                    # Clean data is invoked inside this due to the way we
                    # munge the data and force the types to adhere to the
                    # specified type
                    result = self.textfsm_data(
                        in_info, tfsm_template, self.schema, data
                    )
            else:
                self.logger.error(
                    "{}: No normalization/textfsm function for device {}"
                    .format(self.name, data["hostname"]))
        else:
            d = data.get("data", None)
            err = ""
            if d and isinstance(d, dict):
                err = d.get("error", "")
            self.logger.error(
                "{}: failed for node {} with {}/{}".format(
                    self.name, data["hostname"], data["status"],
                    err
                )
            )

        return result

    def clean_data(self, processed_data, raw_data):

        # Build default data structure
        schema_rec = {}
        def_vals = {
            pa.string(): "-",
            pa.int32(): 0,
            pa.int64(): 0,
            pa.float64(): 0,
            pa.float32(): 0.0,
            pa.bool_(): False,
            pa.date64(): 0.0,
            pa.list_(pa.string()): ["-"],
            pa.list_(pa.int64()): [0],
            pa.list_(pa.struct([('nexthop', pa.string()),
                                ('oif', pa.string()),
                                ('weight', pa.int32())])): [("", "", 1)],
        }
        for field in self.schema:
            default = def_vals[field.type]
            schema_rec.update({field.name: default})

        for entry in processed_data:
            entry.update({"hostname": raw_data["hostname"]})
            entry.update({"namespace": raw_data["namespace"]})
            entry.update({"timestamp": raw_data["timestamp"]})
            for fld in schema_rec:
                if fld not in entry:
                    if fld == "active":
                        entry.update({fld: True})
                    else:
                        entry.update({fld: schema_rec[fld]})

        return processed_data

    async def commit_data(self, result, namespace, hostname):
        """Write the result data out"""
        records = []
        prev_res = self.previous_results.get(hostname, None)

        if result or prev_res:
            adds, dels = self.get_diff(prev_res, result)

            if adds or dels:
                self.previous_results[hostname] = result
                for entry in adds:
                    records.append(entry)
                for entry in dels:
                    if entry.get("active", True):
                        # If there's already an entry marked as deleted
                        # No point in adding one more
                        entry.update({"active": False})
                        entry.update(
                            {"timestamp":
                             int(datetime.utcnow().timestamp() * 1000)}
                        )
                        records.append(entry)

            if records:
                self.write_queue.put_nowait(
                    {
                        "records": records,
                        "topic": self.name,
                        "schema": self.schema,
                        "partition_cols": self.partition_cols,
                    }
                )

    async def schedule(self):
        """This schedules the commands to run"""
        self.logger.info(f"starting service {self.name}")
        while True:
            await self.schedule_commands()
            await asyncio.sleep(self.period + (random.randint(0, 1000) / 1000))

    async def run(self):
        """Start the service"""
        self.logger.info(f"running service {self.name} ")
        self.nodelist = list(self.nodes.keys())

        while True:
            # TODO: the first data is now 15 seconds late, because we don't wait to run
            #   the commands until they are gathered, so this gather after schedule happens too fast

            outputs = await self.gather_data()
            if self.run_once == "gather":
                print(self._dump_output(outputs))
                sys.exit(0)
            elif self.run_once == "process":
                poutputs = []
            for output in outputs:
                if not output:
                    # output from nodes not running service
                    continue
                result = self.process_data(output[0])
                # If a node from init state to good state, hostname will change
                # So fix that in the node list
                if self.run_once == "process":
                    poutputs += [{"namespace": output[0]["namespace"],
                                  "hostname": output[0]["hostname"],
                                  "output": result}]
                else:
                    await self.commit_data(
                        result, output[0]["namespace"], output[0]["hostname"]
                    )

            if self.run_once == "process":
                print(self._dump_output(poutputs))
                sys.exit(0)

            await asyncio.sleep(1 + (random.randint(0, 1000) / 1000))

