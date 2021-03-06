from datetime import datetime
from suzieq.poller.services.service import Service


class OspfNbrService(Service):
    """OSPF Neighbor service. Output needs to be munged"""

    def frr_convert_reltime_to_epoch(self, reltime):
        """Convert string of type 1d12h3m23s into absolute epoch"""
        secs = 0
        s = reltime
        if not reltime:
            return 0

        for t, mul in {
            "w": 3600 * 24 * 7,
            "d": 3600 * 24,
            "h": 3600,
            "m": 60,
            "s": 1,
        }.items():
            v = s.split(t)
            if len(v) == 2:
                secs += int(v[0]) * mul
            s = v[-1]

        return int((datetime.utcnow().timestamp() - secs) * 1000)

    def clean_data(self, processed_data, raw_data):

        dev_type = raw_data.get("devtype", None)
        if dev_type == "cumulus" or dev_type == "linux":
            for entry in processed_data:
                entry["vrf"] = "default"
                entry["state"] = entry["state"].lower()
                entry["lastUpTime"] = self.frr_convert_reltime_to_epoch(
                    entry["lastUpTime"]
                )
                entry["lastDownTime"] = self.frr_convert_reltime_to_epoch(
                    entry["lastDownTime"]
                )
                if entry["lastUpTime"] > entry["lastDownTime"]:
                    entry["lastChangeTime"] = entry["lastUpTime"]
                else:
                    entry["lastChangeTime"] = entry["lastDownTime"]
                entry["areaStub"] = entry["areaStub"] == "[Stub]"
                if not entry["bfdStatus"]:
                    entry["bfdStatus"] = "disabled"
        elif dev_type == "eos":
            for entry in processed_data:
                entry["state"] = entry["state"].lower()
                entry["lastChangeTime"] = int(entry["lastChangeTime"] * 1000)
                # What is provided is the opposite of stub and so we not it
                entry["areaStub"] = not entry["areaStub"]

        return super().clean_data(processed_data, raw_data)
