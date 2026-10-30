from __future__ import annotations

from datetime import time
import logging
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigFlowResult, OptionsFlowWithReload
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers.selector import SelectSelector, SelectSelectorConfig, SelectSelectorMode

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
        )
    )


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 2

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            entity_id = user_input[CONF_ENTITY_ID].strip().lower()
            user_input[CONF_ENTITY_ID] = entity_id

            if not _valid_entity_id(entity_id):
                errors["base"] = "invalid_entity_id"
            else:
                client = None
                try:
                    _LOGGER.debug(
                        "DEBUG config_flow: logging in, tenant=%s",
                        user_input.get(CONF_TENANT_URL, DEFAULT_TENANT_URL),
                    )
                    client = Skola24Client(
                        user_input[CONF_USERNAME],
                        user_input[CONF_PASSWORD],
                        user_input.get(CONF_TENANT_URL, DEFAULT_TENANT_URL),
                    )
                    await self.hass.async_add_executor_job(client.login)
                except Exception:
                    _LOGGER.exception("DEBUG config_flow: login failed")
                    errors["base"] = "cannot_connect"
                else:
                    schema_id = str(user_input.get(CONF_SCHEMA_ID, "")).strip()
                    
                    # Om inget SchemaID/GUID angavs, försök med automatisk sökning
                    if not schema_id and client:
                        try:
                            disc_result = await self.hass.async_add_executor_job(
                                client.discover_students
                            )
                            found_students = disc_result.get("students", [])
                            if found_students:
                                # Välj första eleven om sökningen lyckades
                                schema_id = found_students[0]["schema_id"]
                                user_input[CONF_SCHEMA_ID] = schema_id
                                user_input[CONF_SELECTION] = found_students[0]["selection"]
                                user_input["selection_type"] = found_students[0]["selection_type"]
                        except Exception:
                            pass

                    if not schema_id:
                        # Om det fortfarande är tomt sätter vi ett tydligt fel i formuläret
                        errors["base"] = "missing_schema_id"
                    else:
                        if ":" in schema_id or len(schema_id) >= 20:
                            raw_sel = schema_id.split(":", 1)[1].strip() if ":" in schema_id else schema_id
                            user_input[CONF_SELECTION] = client._selection_token(raw_sel)
                            user_input["selection_type"] = 5
                            _LOGGER.debug(
                                "DEBUG config_flow: using direct selection (selectionType 5)"
                            )
                        else:
                            try:
                                signature = await self.hass.async_add_executor_job(
                                    client._schema_signature, schema_id
                                )
                                user_input[CONF_SELECTION] = signature
                                user_input["selection_type"] = 4
                                _LOGGER.debug(
                                    "DEBUG config_flow: signature created from SchemaID"
                                )
                            except Exception:
                                _LOGGER.exception(
                                    "DEBUG config_flow: _schema_signature failed"
                                )
                                errors["base"] = "invalid_schema_id"

                    if not errors:
                        from urllib.parse import urlparse
                        tenant_host = (urlparse(user_input.get(CONF_TENANT_URL, DEFAULT_TENANT_URL)).hostname or "skola24").lower()
                        await self.async_set_unique_id(f"{tenant_host}_{entity_id}")
                        self._abort_if_unique_id_configured()
                        
                        return self.async_create_entry(
                            title=f"Skola24 – {user_input[CONF_STUDENT_NAME]}",
                            data=user_input,
                        )

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_TENANT_URL, default=DEFAULT_TENANT_URL): _tenant_selector(),
                    vol.Required(CONF_STUDENT_NAME, default="Elev"): str,
                    vol.Required(CONF_ENTITY_ID, default="skola24_elev"): str,
                    vol.Optional(CONF_SCHEMA_ID, default=""): str,
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            errors=errors,
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
            else:
                return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=self._schema(),
            errors=errors,
        )