from suzieq.poller.services.service import Service


class ArpndService(Service):
    """arpnd service. Different class because minor munging of output"""

    def clean_data(self, processed_data, raw_data):

        devtype = raw_data.get("devtype", None)
        if any([devtype == x for x in ["cumulus", "linux"]]):
            for entry in processed_data:
                entry["offload"] = entry["offload"] == "offload"
                entry["state"] = entry["state"].lower()
                if entry["state"] == "stale" or entry["state"] == "delay":
                    entry["state"] = "reachable"
        elif devtype == 'eos':
            for entry in processed_data:
                entry['macaddr'] = ':'.join(
                    [f'{x[:2]}:{x[2:]}' for x in entry['macaddr'].split('.')])

        return super().clean_data(processed_data, raw_data)
