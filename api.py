from __future__ import annotations

import base64
import html
import json
import logging
import re
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import requests

from .const import (
    BASE_URL,
    PERSONAL_SELECTION_TYPE,
    PUBLIC_SELECTION_TYPE,
    DEFAULT_TENANT_URL,
    LOGIN_PATH,
    UNIT_GUID,
    USER_AGENT,
    VIEWER_PATH,
    X_SCOPE,
)

_LOGGER = logging.getLogger(__name__)


class FormParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.forms, self.current = [], None
        self.current_select, self.current_option = None, None

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        tag = tag.lower()

        if tag == "form":
            self.current = {
                "action": a.get("action", ""),
                "inputs": [],
                "selects": [],
            }
            self.forms.append(self.current)
        elif tag == "input" and self.current is not None:
            self.current["inputs"].append(a)
        elif tag == "select" and self.current is not None:
            self.current_select = {
                "name": a.get("name", ""),
                "options": [],
            }
        elif tag == "option" and self.current_select is not None:
            self.current_option = a
            self.current_select["options"].append({
                "value": a.get("value", ""),
                "text": "",
                "selected": "selected" in a,
            })

    def handle_data(self, data):
        if self.current_select is not None and self.current_option is not None:
            self.current_select["options"][-1]["text"] += data.strip()

    def handle_endtag(self, tag):
        tag = tag.lower()

        if tag == "option":
            self.current_option = None
        elif tag == "select":
            self.current["selects"].append(self.current_select)
            self.current_select = None
        elif tag == "form":
            self.current = None


class Skola24Error(Exception):
    pass


class Skola24Client:
    def __init__(self, username, password, tenant_url=DEFAULT_TENANT_URL):
        self.username, self.password = username, password
        self.tenant_url = str(tenant_url or DEFAULT_TENANT_URL).strip()

        if not self.tenant_url.endswith("/"):
            self.tenant_url += "/"

        self.host = (urlparse(self.tenant_url).hostname or "").lower()

        if not self.host:
            raise Skola24Error("Ogiltig Skola24-domän.")

        self.login_url = urljoin(self.tenant_url, LOGIN_PATH)
        self.viewer_url = (
            "https://web.skola24.se/portal/start/timetable/"
            "timetable-viewer/" + self.host + "/"
        )

        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept-Language": "sv-SE,sv;q=0.9,en-US;q=0.8,en;q=0.7",
        })

    def login(self):
        self.session.get(self.tenant_url, timeout=30).raise_for_status()

        page = self.session.get(self.login_url, timeout=30)
        page.raise_for_status()

        parser = FormParser()
        parser.feed(page.text)

        form = next((
            f for f in parser.forms
            if any(
                (x.get("type") or "").lower() == "password"
                for x in f["inputs"]
            )
        ), None)

        if not form:
            raise Skola24Error("Hittade inget Skola24-loginformulär.")

        pw = next(
            x for x in form["inputs"]
            if (x.get("type") or "").lower() == "password"
        )
        idx = form["inputs"].index(pw)

        user = next((
            x for x in reversed(form["inputs"][:idx])
            if x.get("name")
            and (x.get("type") or "text").lower() in ("text", "email")
        ), None)

        if not user:
            raise Skola24Error("Hittade inget användarnamnsfält.")

        data = {}

        for x in form["inputs"]:
            if x.get("name") and (x.get("type") or "").lower() == "hidden":
                data[x["name"]] = html.unescape(x.get("value", ""))

        for sel in form["selects"]:
            chosen = next((
                o for o in sel["options"]
                if self.host in o["text"].lower()
            ), None)

            if chosen is None:
                chosen = next(
                    (o for o in sel["options"] if o["selected"]),
                    None,
                )

            if chosen:
                data[sel["name"]] = chosen["value"]

        data[user["name"]] = self.username
        data[pw["name"]] = self.password

        submit = next((
            x for x in form["inputs"]
            if (x.get("type") or "").lower() == "submit"
            and x.get("name")
        ), None)

        if submit:
            data[submit["name"]] = submit.get("value", "")

        action = urljoin(page.url, form["action"] or page.url)

        r = self.session.post(
            action,
            data=data,
            allow_redirects=True,
            timeout=30,
        )
        r.raise_for_status()

        if (
            "login.aspx" in r.url.lower()
            or "defaulterrorpage.aspx" in r.url.lower()
        ):
            raise Skola24Error("Skola24-inloggningen misslyckades.")

    @staticmethod
    def _selection_token(value):
        """Normalize a selection value to Skola24's base64 string form."""
        if not value:
            return ""

        value = str(value).strip()

        try:
            decoded = base64.b64decode(
                value,
                validate=True,
            ).decode("utf-8")

            if len(decoded) >= 20 and "-" in decoded:
                return value
        except Exception:
            pass

        if len(value) >= 20 and "-" in value:
            return base64.b64encode(
                value.encode("utf-8")
            ).decode("ascii")

        return value

    @staticmethod
    def _strip_html(text):
        try:
            class _Text(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.parts = []

                def handle_data(self, data):
                    if data and data.strip():
                        self.parts.append(data.strip())

            parser = _Text()
            parser.feed(text)
            return " ".join(parser.parts)
        except Exception:
            return re.sub(r"<[^>]+>", " ", text)

    @staticmethod
    def _extract_schema_ids(text):
        visible = Skola24Client._strip_html(text)
        found = []
        patterns = [
            re.compile(
                r"([A-ZÅÄÖ][A-Za-zÅÄÖåäöÉé'’.-]*"
                r"(?:\s+[A-ZÅÄÖ][A-Za-zÅÄÖåäöÉé'’.-]*){1,4})"
                r"\s+SchemaID\s*:\s*([A-Za-z0-9_-]{4,64})",
                re.I,
            ),
        ]
        for pat in patterns:
            for match in pat.finditer(visible):
                if len(match.groups()) == 2:
                    name, schema_id = match.groups()
                    found.append({"name": name.strip(), "schema_id": schema_id.strip()})
        return found

    def _schema_signature(self, schema_id):
        r = self.session.post(
            f"{BASE_URL}/api/encrypt/signature",
            json={"signature": schema_id},
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-Scope": X_SCOPE,
                "Referer": self.tenant_url,
                "Origin": BASE_URL,
                "User-Agent": USER_AGENT,
            },
            timeout=20,
        )
        r.raise_for_status()
        payload = r.json()
        data = payload.get("data", {}) if isinstance(payload, dict) else {}
        signature = data if isinstance(data, str) else data.get("signature")
        if not signature:
            raise Skola24Error("Skola24 returnerade ingen signature för SchemaID.")
        return str(signature)

    PERSONAL_TIMETABLES_URL = f"{BASE_URL}/api/services/skola24/get/personal/timetables"

    @staticmethod
    def _extract_students(payload):
        """Plocka ut barn/elever ur svaret från get/personal/timetables."""
        resp = ((payload or {}).get("data") or {}).get("getPersonalTimetablesResponse") or {}
        found = {}

        for key in ("childrenTimetables", "studentTimetables"):
            for item in resp.get(key) or []:
                if not isinstance(item, dict):
                    continue
                guid = item.get("personGuid")
                if not guid:
                    continue
                unit = item.get("unitGuid") or ""
                name = " ".join(
                    p for p in (item.get("firstName"), item.get("lastName")) if p
                ) or str(guid)
                found[guid] = {
                    "name": name,
                    "person_guid": guid,
                    "school": item.get("schoolID") or "",
                    "timetable_id": item.get("timetableID") or "",
                    # unit:guid => render_week använder rätt skolenhet
                    "schema_id": f"{unit}:{guid}" if unit else guid,
                    "selection": Skola24Client._selection_token(guid),
                    "selection_type": PUBLIC_SELECTION_TYPE,
                }
        return list(found.values())

    def discover_students(self, login=True):
        """Hämta inloggad vårdnadshavares barn via Skola24:s personliga scheman."""
        if login:
            self.login()

        self.session.get(
            self.viewer_url,
            headers={"Referer": self.tenant_url, "User-Agent": USER_AGENT},
            timeout=30,
        ).raise_for_status()

        headers = {
            "Accept": "application/json, text/javascript, */*; q=0.01",
            "Content-Type": "application/json",
            "X-Requested-With": "XMLHttpRequest",
            "X-Scope": X_SCOPE,
            "Referer": self.viewer_url,
            "Origin": BASE_URL,
            "User-Agent": USER_AGENT,
        }

        # Payloaden är inte bekräftad - prova de troliga varianterna i tur och ordning.
        payloads = (
            {"getPersonalTimetablesRequest": {"hostName": self.host}},
            {"hostName": self.host},
            {},
        )

        last_error = None
        for body in payloads:
            try:
                r = self.session.post(
                    self.PERSONAL_TIMETABLES_URL, json=body, headers=headers, timeout=20
                )
                r.raise_for_status()
                payload = r.json()
            except (requests.RequestException, ValueError) as err:
                last_error = err
                _LOGGER.debug("get/personal/timetables misslyckades med %s: %s", body, err)
                continue

            students = self._extract_students(payload)
            if students:
                return {"students": students}

        if last_error:
            _LOGGER.warning("Kunde inte hämta barn automatiskt: %s", last_error)
        else:
            _LOGGER.warning("Skola24 returnerade inga barn för kontot.")
        return {"students": []}

    def _active_school_year(self):
        r = self.session.post(
            f"{BASE_URL}/api/get/active/school/years",
            json={"hostName": self.host, "checkSchoolYearsFeatures": False},
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-Scope": X_SCOPE,
                "Referer": self.viewer_url,
                "Origin": BASE_URL,
                "User-Agent": USER_AGENT,
            },
            timeout=20,
        )
        r.raise_for_status()
        payload = r.json()
        years = (payload.get("data") or {}).get("activeSchoolYears")
        if not years:
            raise Skola24Error("Kunde inte hämta aktuellt läsår från Skola24.")
        guid = years[0].get("guid")
        if not guid:
            raise Skola24Error("Skola24 returnerade inget aktuellt schoolYear-guid.")
        return str(guid)

    def render_week(self, week, year, schema_id=None, selection=None, selection_type=None):
        self.login()
        self.session.get(
            self.viewer_url,
            headers={"Referer": self.tenant_url, "User-Agent": USER_AGENT},
            timeout=30,
        ).raise_for_status()

        r = self.session.post(
            f"{BASE_URL}/api/get/timetable/render/key",
            json={},
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-Scope": X_SCOPE,
                "Referer": self.viewer_url,
                "Origin": BASE_URL,
                "User-Agent": USER_AGENT,
            },
            timeout=30,
        )
        r.raise_for_status()
        d = r.json()
        key = (d.get("data") or {}).get("key") or d.get("key") or ""
        if not key:
            raise Skola24Error("Kunde inte hämta render key.")

        unit_guid = UNIT_GUID
        raw_schema_id = str(schema_id).strip() if schema_id else ""

        if ":" in raw_schema_id:
            parts = raw_schema_id.split(":", 1)
            unit_guid = self._selection_token(parts[0].strip())
            raw_schema_id = parts[1].strip()

        if raw_schema_id and len(raw_schema_id) >= 20:
            selection = self._selection_token(raw_schema_id)
            selection_type = PUBLIC_SELECTION_TYPE
        elif raw_schema_id:
            selection = self._schema_signature(raw_schema_id)
            selection_type = PERSONAL_SELECTION_TYPE
        elif selection:
            selection_type = int(selection_type or PUBLIC_SELECTION_TYPE)
        else:
            raise Skola24Error("Ingen SchemaID eller schema-selection är konfigurerad.")

        school_year = self._active_school_year()
        is_type_5 = int(selection_type) == PUBLIC_SELECTION_TYPE

        payload = {
            "renderKey": key,
            "host": self.host,
            "unitGuid": unit_guid,
            "schoolYear": school_year,
            "startDate": None,
            "endDate": None,
            "scheduleDay": 0,
            "blackAndWhite": False,
            "width": 125,
            "height": 550,
            "selectionType": int(selection_type),
            "selection": selection,
            "personalTimetable": True if is_type_5 else False,
            "showHeader": False,
            "periodText": "",
            "week": week,
            "year": year,
            "privateFreeTextMode": False,
            "privateSelectionMode": True if is_type_5 else None,
            "customerKey": "",
        }

        r = self.session.post(
            f"{BASE_URL}/api/render/timetable",
            json=payload,
            headers={
                "Accept": "application/json, text/javascript, */*; q=0.01",
                "Content-Type": "application/json",
                "X-Requested-With": "XMLHttpRequest",
                "X-Scope": X_SCOPE,
                "Referer": self.viewer_url,
                "Origin": BASE_URL,
                "User-Agent": USER_AGENT,
            },
            timeout=30,
        )
        r.raise_for_status()
        response_payload = r.json()
        extracted_lessons = self.extract(response_payload)
        
        # INGEN KONTROLL HÄR SOM KRÄVER LEKTIONER - Tillåt tomma lovveckor!
        return self.normalize(extracted_lessons)

    @staticmethod
    def extract(obj):
        found = []

        def walk(v):
            if isinstance(v, dict):
                for key in ["lessonInfo", "lessonList", "lessons", "boxList", "columns"]:
                    if isinstance(v.get(key), list):
                        for item in v[key]:
                            if isinstance(item, dict):
                                found.append(item)

                if (
                    isinstance(v.get("texts") or v.get("lessonTexts"), list)
                    and ("timeStart" in v or "startTime" in v or "time" in v)
                ):
                    found.append(v)

                for x in v.values():
                    walk(x)

            elif isinstance(v, list):
                for x in v:
                    walk(x)

        walk(obj)

        unique = {}
        for x in found:
            t = x.get("texts") or x.get("lessonTexts") or []
            start = x.get("timeStart") or x.get("startTime") or ""
            end = x.get("timeEnd") or x.get("endTime") or ""
            day = x.get("dayOfWeekNumber") if x.get("dayOfWeekNumber") is not None else x.get("day", 0)
            
            subject = t[0] if len(t) > 0 and t[0] else ""

            # Om både ämne och starttid saknas är det en tom platshållare -> hoppa över
            if not subject and not start:
                continue

            key = (
                x.get("guidId") or x.get("id"),
                day,
                start,
                end,
                json.dumps(t, ensure_ascii=False),
            )
            unique[key] = x

        return list(unique.values())

    @staticmethod
    def normalize(lessons):
        result = []

        for x in lessons:
            t = x.get("texts") or x.get("lessonTexts") or []
            start = x.get("timeStart") or x.get("startTime") or ""
            end = x.get("timeEnd") or x.get("endTime") or ""
            day = x.get("dayOfWeekNumber") if x.get("dayOfWeekNumber") is not None else x.get("day", 0)

            result.append({
                "subject": t[0] if len(t) > 0 else "",
                "teacher": t[1] if len(t) > 1 else "",
                "room": t[2] if len(t) > 2 else "",
                "start": start,
                "end": end,
                "timeStart": start,
                "timeEnd": end,
                "day_of_week": day,
                "dayOfWeekNumber": day,
                "guidId": x.get("guidId", x.get("id", "")),
                "blockName": x.get("blockName", ""),
            })

        return result