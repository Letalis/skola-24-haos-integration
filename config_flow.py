from __future__ import annotations

from datetime import time
import logging
from typing import Any
from urllib.parse import urlparse

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode
from homeassistant.util import slugify

from .api import Skola24Client
from .domains import SKOLA24_DOMAINS
from .const import (
    CONF_ENTITY_ID,
    CONF_SELECTION,
    CONF_AVAILABLE_STUDENTS,
    CONF_SCHEMA_ID,
    CONF_STUDENT_NAME,
    CONF_TIME,
    CONF_UPDATE_INTERVAL,
    CONF_TENANT_URL,
    DEFAULT_TENANT_URL,
    CONF_WEEKDAY,
    CONF_WEEKS_AHEAD,
    DEFAULT_TIME,
    DEFAULT_UPDATE_INTERVAL,
    DEFAULT_WEEKDAY,
    DEFAULT_WEEKS_AHEAD,
    DOMAIN,
    WEEKDAY_NAMES,
)


_LOGGER = logging.getLogger(__name__)


def _valid_entity_id(value: str) -> bool:
    value = value.strip().lower()
    return bool(value) and all(c.isalnum() or c == "_" for c in value)


def _valid_time(value: str) -> bool:
    try:
        time.fromisoformat(value.strip())
        return True
    except ValueError:
        return False


def _tenant_selector():
    return SelectSelector(
        SelectSelectorConfig(
            options=[
                {"value": f"https://{host}/", "label": host}
                for _domain_id, host in SKOLA24_DOMAINS
            ],
            mode=SelectSelectorMode.DROPDOWN,
            sort=True,
        )
    )


async def _resolve_selection(hass, client: Skola24Client, schema_id: str) -> tuple[str, int]:
    """Gör om ett SchemaID/GUID till (selection, selection_type)."""
    if ":" in schema_id or len(schema_id) >= 20:
        raw_sel = schema_id.split(":", 1)[1].strip() if ":" in schema_id else schema_id
        return Skola24Client._selection_token(raw_sel), 5
    signature = await hass.async_add_executor_job(client._schema_signature, schema_id)
    return signature, 4


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    def __init__(self) -> None:
        self._creds: dict[str, Any] = {}
        self._students: list[dict[str, Any]] = []
        self._client: Skola24Client | None = None

    # Steg 1: domän + inloggning, därefter automatisk sökning av barn
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            try:
                client = Skola24Client(
                    user_input[CONF_USERNAME],
                    user_input[CONF_PASSWORD],
                    user_input[CONF_TENANT_URL],
                )
                await self.hass.async_add_executor_job(client.login)
            except Exception:
                _LOGGER.exception("config_flow: login failed")
                errors["base"] = "cannot_connect"
            else:
                self._creds = dict(user_input)
                self._client = client
                try:
                    result = await self.hass.async_add_executor_job(
                        client.discover_students, False
                    )
                    self._students = result.get("students", [])
                except Exception:
                    _LOGGER.exception("config_flow: student discovery failed")
                    self._students = []

                if self._students:
                    return await self.async_step_student()
                return await self.async_step_manual()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TENANT_URL): _tenant_selector(),
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
        )

    # Steg 2a: välj barn bland de som hittades
    async def async_step_student(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        by_guid = {s["person_guid"]: s for s in self._students}

        if user_input is not None:
            s = by_guid[user_input["student"]]
            return await self._finish(
                name=s["name"],
                entity_id=f"skola24_{slugify(s['name'])}",
                schema_id=s["schema_id"],
                selection=s["selection"],
                selection_type=s["selection_type"],
                unique=s["person_guid"],
            )

        return self.async_show_form(
            step_id="student",
            data_schema=vol.Schema(
                {
                    vol.Required("student", default=next(iter(by_guid))): SelectSelector(
                        SelectSelectorConfig(
                            options=[
                                {
                                    "value": guid,
                                    "label": f"{s['name']} ({s['school']})" if s.get("school") else s["name"],
                                }
                                for guid, s in by_guid.items()
                            ],
                            mode=SelectSelectorMode.DROPDOWN,
                        )
                    )
                }
            ),
        )

    # Steg 2b: reservväg om inget barn hittades - ange SchemaID/GUID manuellt
    async def async_step_manual(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            entity_id = user_input[CONF_ENTITY_ID].strip().lower()
            schema_id = str(user_input.get(CONF_SCHEMA_ID, "")).strip()

            if not _valid_entity_id(entity_id):
                errors["base"] = "invalid_entity_id"
            elif not schema_id:
                errors["base"] = "missing_schema_id"
            else:
                try:
                    selection, selection_type = await _resolve_selection(
                        self.hass, self._client, schema_id
                    )
                except Exception:
                    _LOGGER.exception("config_flow: SchemaID verification failed")
                    errors["base"] = "invalid_schema_id"
                else:
                    return await self._finish(
                        name=user_input[CONF_STUDENT_NAME],
                        entity_id=entity_id,
                        schema_id=schema_id,
                        selection=selection,
                        selection_type=selection_type,
                        unique=entity_id,
                    )

        return self.async_show_form(
            step_id="manual",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_STUDENT_NAME, default="Elev"): str,
                    vol.Required(CONF_ENTITY_ID, default="skola24_elev"): str,
                    vol.Required(CONF_SCHEMA_ID): str,
                }
            ),
            errors=errors,
        )

    async def _finish(
        self,
        *,
        name: str,
        entity_id: str,
        schema_id: str,
        selection: str,
        selection_type: int,
        unique: str,
    ) -> ConfigFlowResult:
        tenant_url = self._creds[CONF_TENANT_URL]
        tenant_host = (urlparse(tenant_url).hostname or "skola24").lower()

        await self.async_set_unique_id(f"{tenant_host}_{unique}")
        self._abort_if_unique_id_configured()

        return self.async_create_entry(
            title=f"Skola24 – {name}",
            data={
                CONF_TENANT_URL: tenant_url,
                CONF_USERNAME: self._creds[CONF_USERNAME],
                CONF_PASSWORD: self._creds[CONF_PASSWORD],
                CONF_STUDENT_NAME: name,
                CONF_ENTITY_ID: entity_id,
                CONF_SCHEMA_ID: schema_id,
                CONF_SELECTION: selection,
                "selection_type": selection_type,
                CONF_AVAILABLE_STUDENTS: self._students,
            },
        )

    @staticmethod
    def async_get_options_flow(config_entry: config_entries.ConfigEntry):
        return OptionsFlowHandler()


class OptionsFlowHandler(OptionsFlowWithReload):
    def __init__(self):
        super().__init__()
        self._students: list[dict[str, Any]] = []

    def _schema(self):
        current = self.config_entry.options
        data = self.config_entry.data
        students = self._students or current.get(CONF_AVAILABLE_STUDENTS, []) or data.get(CONF_AVAILABLE_STUDENTS, [])
        student_map = {
            str(s.get("name")): str(s.get("schema_id"))
            for s in students
            if s.get("name") and s.get("schema_id")
        }
        schema = {
            vol.Required(CONF_TENANT_URL, default=current.get(CONF_TENANT_URL, data.get(CONF_TENANT_URL, DEFAULT_TENANT_URL))): _tenant_selector(),
            vol.Required(
                CONF_ENTITY_ID,
                default=current.get(CONF_ENTITY_ID, data.get(CONF_ENTITY_ID, "skola24_elev")),
            ): str,
            vol.Required(
                CONF_WEEKDAY,
                default=current.get(CONF_WEEKDAY, data.get(CONF_WEEKDAY, DEFAULT_WEEKDAY)),
            ): vol.In(WEEKDAY_NAMES),
            vol.Required(
                CONF_TIME,
                default=current.get(CONF_TIME, data.get(CONF_TIME, DEFAULT_TIME)),
            ): str,
            vol.Required(
                CONF_WEEKS_AHEAD,
                default=current.get(CONF_WEEKS_AHEAD, data.get(CONF_WEEKS_AHEAD, DEFAULT_WEEKS_AHEAD)),
            ): vol.All(vol.Coerce(int), vol.Range(min=0, max=8)),
            vol.Required(
                CONF_UPDATE_INTERVAL,
                default=current.get(CONF_UPDATE_INTERVAL, data.get(CONF_UPDATE_INTERVAL, DEFAULT_UPDATE_INTERVAL)),
            ): vol.All(vol.Coerce(int), vol.Range(min=5, max=360)),
        }
        if student_map:
            current_schema_id = current.get(CONF_SCHEMA_ID, data.get(CONF_SCHEMA_ID, next(iter(student_map.values()))))
            reverse = {v: k for k, v in student_map.items()}
            default_schema_id = current_schema_id if current_schema_id in reverse else next(iter(student_map.values()))
            schema[vol.Required("selected_student", default=default_schema_id)] = SelectSelector(
                SelectSelectorConfig(
                    options=[
                        {"value": value, "label": name}
                        for name, value in student_map.items()
                    ],
                    mode=SelectSelectorMode.DROPDOWN,
                )
            )
        else:
            schema[vol.Required(
                CONF_STUDENT_NAME,
                default=current.get(CONF_STUDENT_NAME, data.get(CONF_STUDENT_NAME, "Elev")),
            )] = str
            schema[vol.Required(
                CONF_SCHEMA_ID,
                default=current.get(CONF_SCHEMA_ID, data.get(CONF_SCHEMA_ID, "")),
            )] = str
        return vol.Schema(schema)

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is None and not (
            self.config_entry.options.get(CONF_AVAILABLE_STUDENTS)
            or self.config_entry.data.get(CONF_AVAILABLE_STUDENTS)
        ):
            try:
                result = await self.hass.async_add_executor_job(
                    self.hass.data[DOMAIN][self.config_entry.entry_id].client.discover_students
                )
                students = result.get("students", [])
                self._students = students
                if students:
                    options = dict(self.config_entry.options)
                    options[CONF_AVAILABLE_STUDENTS] = students
                    self.hass.config_entries.async_update_entry(self.config_entry, options=options)
            except Exception:
                _LOGGER.exception("DEBUG options_flow: student discovery failed")

        if user_input is not None:
            entity_id = user_input[CONF_ENTITY_ID].strip().lower()
            user_input[CONF_ENTITY_ID] = entity_id
            selected_value = user_input.pop("selected_student", None)
            if selected_value:
                students = self._students or self.config_entry.options.get(CONF_AVAILABLE_STUDENTS, []) or self.config_entry.data.get(CONF_AVAILABLE_STUDENTS, [])
                selected = next(
                    (s for s in students if str(s.get("schema_id")) == str(selected_value)),
                    None,
                )
                if selected:
                    user_input[CONF_STUDENT_NAME] = selected["name"]
                    user_input[CONF_SCHEMA_ID] = selected["schema_id"]
                    user_input[CONF_SELECTION] = selected.get("selection")
                    user_input["selection_type"] = int(selected.get("selection_type", 4))
            schema_id = str(user_input.get(CONF_SCHEMA_ID, "")).strip()
            if schema_id:
                if ":" in schema_id or len(schema_id) >= 20:
                    raw_sel = schema_id.split(":", 1)[1].strip() if ":" in schema_id else schema_id
                    user_input[CONF_SELECTION] = Skola24Client._selection_token(raw_sel)
                    user_input["selection_type"] = 5
                else:
                    try:
                        client = Skola24Client(
                            self.config_entry.data.get(CONF_USERNAME),
                            self.config_entry.data.get(CONF_PASSWORD),
                            user_input.get(CONF_TENANT_URL, DEFAULT_TENANT_URL),
                        )
                        def _verify():
                            client.login()
                            return client._schema_signature(schema_id)
                        signature = await self.hass.async_add_executor_job(_verify)
                        user_input[CONF_SELECTION] = signature
                        user_input["selection_type"] = 4
                    except Exception:
                        _LOGGER.exception("DEBUG options_flow: SchemaID verification failed")
                        errors["base"] = "invalid_schema_id"
            if not _valid_entity_id(entity_id):
                errors["base"] = "invalid_entity_id"
            elif not _valid_time(str(user_input[CONF_TIME])):
                errors["base"] = "invalid_time"

            if not errors:
                return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self._schema(),
            errors=errors,
        )