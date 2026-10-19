"""Set up a CrewBrain account with its address, username and password.

The password buys an access token and is dropped. Only the token is stored.
"""

from __future__ import annotations

from collections.abc import Mapping
import logging
import re
from typing import Any

import voluptuous as vol
from yarl import URL

from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlowWithReload,
)
from homeassistant.const import CONF_API_TOKEN, CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    SERVICE_DOMAIN,
    CrewBrainApi,
    CrewBrainAuthError,
    CrewBrainError,
    category_title,
    employee_id_of,
    is_crewbrain_host,
)
from .const import (
    CONF_BASE_URL,
    CONF_EMPLOYEE_ID,
    DEFAULT_ACTIVITY,
    DEFAULT_DAYS_AHEAD,
    DOMAIN,
    OPT_ACTIVITY,
    OPT_CATEGORY,
    OPT_DAYS_AHEAD,
)

_LOGGER = logging.getLogger(__name__)

_USERNAME = TextSelector(TextSelectorConfig(
    type=TextSelectorType.TEXT, autocomplete="username"))
_PASSWORD = TextSelector(TextSelectorConfig(
    type=TextSelectorType.PASSWORD, autocomplete="current-password"))
_HOST = re.compile(r"[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+")


class _NoEmployee(Exception):
    """`/v2/me` answered without an employee ID."""


def normalise_url(text: str) -> str | None:
    """`yourcompany`, `yourcompany.crewbrain.com` or a URL, as an origin.

    Only CrewBrain's own hosts: the password goes there.
    """
    text = text.strip()
    bare = "://" not in text
    try:
        url = URL(f"https://{text}" if bare else text)
        host = (url.host or "").lower().rstrip(".")
        if bare and host and not is_crewbrain_host(host):
            host = f"{host}.{SERVICE_DOMAIN}"
        if (url.scheme not in ("http", "https") or not _HOST.fullmatch(host)
                or not is_crewbrain_host(host) or host == SERVICE_DOMAIN):
            return None
        return str(url.with_host(host).origin())
    except (TypeError, ValueError):
        return None


async def _sign_in(hass: HomeAssistant, url: str, username: str,
                   password: str) -> dict[str, Any]:
    """A token for the login, who it belongs to, and where CrewBrain answers."""
    api = CrewBrainApi(async_get_clientsession(hass), url)
    token = await api.login(username, password)
    me = await api.me()
    api.employee_id = employee_id_of(me)
    if api.employee_id is None:
        raise _NoEmployee
    # Reading the punch clock proves the token may do what this needs.
    await api.open_stamps()
    name = " ".join(str(me.get(k) or "").strip()
                    for k in ("Firstname", "Lastname")).strip()
    return {
        "token": token,
        "base_url": api.base_url,
        "employee_id": api.employee_id,
        "name": name or str(me.get("Username") or username),
    }


class CrewBrainConfigFlow(ConfigFlow, domain=DOMAIN):
    """Address and login in, one entry per employee out."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            url = normalise_url(user_input[CONF_URL])
            username = user_input[CONF_USERNAME].strip()
            if url is None:
                errors[CONF_URL] = "invalid_url"
            else:
                info, error = await self._try(url, username, user_input[CONF_PASSWORD])
                if error:
                    errors["base"] = error
                else:
                    host = URL(info["base_url"]).host
                    await self.async_set_unique_id(f"{host}_{info['employee_id']}")
                    self._abort_if_unique_id_configured()
                    return self.async_create_entry(
                        title=f"CrewBrain {info['name']}",
                        data={
                            CONF_URL: url,
                            CONF_USERNAME: username,
                            CONF_API_TOKEN: info["token"],
                            CONF_BASE_URL: info["base_url"],
                            CONF_EMPLOYEE_ID: info["employee_id"],
                        },
                    )
        schema = vol.Schema({
            vol.Required(CONF_URL): str,
            vol.Required(CONF_USERNAME): _USERNAME,
            vol.Required(CONF_PASSWORD): _PASSWORD,
        })
        kept = {k: v for k, v in (user_input or {}).items() if k != CONF_PASSWORD}
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(schema, kept),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            info, error = await self._try(entry.data[CONF_URL], username,
                                          user_input[CONF_PASSWORD])
            if error:
                errors["base"] = error
            else:
                await self.async_set_unique_id(
                    f"{URL(info['base_url']).host}_{info['employee_id']}")
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={CONF_USERNAME: username,
                                  CONF_API_TOKEN: info["token"],
                                  CONF_BASE_URL: info["base_url"]},
                )
        schema = vol.Schema({
            vol.Required(CONF_USERNAME,
                         default=entry.data.get(CONF_USERNAME, "")): _USERNAME,
            vol.Required(CONF_PASSWORD): _PASSWORD,
        })
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=schema,
            errors=errors,
            description_placeholders={"name": entry.title},
        )

    async def _try(self, url: str, username: str,
                   password: str) -> tuple[dict[str, Any], str | None]:
        try:
            return await _sign_in(self.hass, url, username, password), None
        except CrewBrainAuthError:
            return {}, "invalid_auth"
        except _NoEmployee:
            return {}, "no_employee"
        except CrewBrainError as err:
            _LOGGER.warning("CrewBrain not reachable: %s", err)
            return {}, "cannot_connect"
        except Exception:
            _LOGGER.exception("Unexpected error signing in to CrewBrain")
            return {}, "unknown"

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> OptionsFlowWithReload:
        return CrewBrainOptionsFlow()


class CrewBrainOptionsFlow(OptionsFlowWithReload):
    """How far ahead jobs are shown, and what a clock-in records.

    Saving reloads the entry. A new token from reauth reloads it too, through
    its own path, so there is no update listener to reload it a second time.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        options = self.config_entry.options
        if user_input is not None:
            return self.async_create_entry(data={**options, **user_input})
        fields: dict[Any, Any] = {
            vol.Required(OPT_DAYS_AHEAD,
                         default=options.get(OPT_DAYS_AHEAD, DEFAULT_DAYS_AHEAD)):
                vol.All(NumberSelector(NumberSelectorConfig(
                    min=1, max=60, step=1, mode=NumberSelectorMode.BOX)),
                    vol.Coerce(int)),
            vol.Required(OPT_ACTIVITY,
                         default=options.get(OPT_ACTIVITY, DEFAULT_ACTIVITY)): str,
        }
        # The categories are read when the entry loads. Not loaded, the
        # chosen one is kept as it is.
        if self.config_entry.state is ConfigEntryState.LOADED:
            data = self.config_entry.runtime_data
            choices = [SelectOptionDict(value=str(c["ID"]), label=category_title(c))
                       for c in data.categories]
            current = str(options.get(OPT_CATEGORY) or data.default_category or "")
            if choices:
                known = current in {c["value"] for c in choices}
                key = (vol.Required(OPT_CATEGORY, default=current) if known
                       else vol.Required(OPT_CATEGORY))
                fields[key] = SelectSelector(SelectSelectorConfig(
                    options=choices, mode=SelectSelectorMode.DROPDOWN))
        return self.async_show_form(step_id="init", data_schema=vol.Schema(fields))
