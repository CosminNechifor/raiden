import fcntl
import os
import sys
import termios
import time
import tty
import uuid
from collections import deque
from contextlib import contextmanager
from datetime import datetime
from itertools import islice
from typing import Union

import requests
import structlog
from eth_utils import to_checksum_address
from requests.adapters import HTTPAdapter
from web3.gas_strategies.time_based import fast_gas_price_strategy, medium_gas_price_strategy

from raiden.network.rpc.client import JSONRPCClient, check_address_has_code
from raiden.network.rpc.smartcontract_proxy import ContractProxy
from raiden_contracts.constants import CONTRACT_CUSTOM_TOKEN
from raiden_contracts.contract_manager import CONTRACT_MANAGER
from scenario_player.exceptions import ScenarioTxError

log = structlog.get_logger(__name__)


# Seriously requests? For Humans?
class TimeOutHTTPAdapter(HTTPAdapter):
    def __init__(self, *args, **kwargs):
        self.timeout = kwargs.pop('timeout', None)
        super().__init__(*args, **kwargs)

    def send(self, *args, **kwargs):
        if 'timeout' not in kwargs or not kwargs['timeout']:
            kwargs['timeout'] = self.timeout
        return super().send(*args, **kwargs)


class FrozenDict(dict):
    """An immutable dict subclass that can be used as a dict key"""
    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._hash = None

    def __hash__(self):
        if self._hash is None:
            h = 0
            for k, v in self.items():
                h ^= hash((k, v))
            self._hash = h
        return self._hash

    def __setitem__(self, k, v) -> None:
        raise TypeError("FrozenDict is immutable")

    def __delitem__(self, v) -> None:
        raise TypeError("FrozenDict is immutable")

    def update(self, *args, **kwargs) -> None:
        raise TypeError("FrozenDict is immutable")

    def clear(self) -> None:
        raise TypeError("FrozenDict is immutable")


class LogBuffer:
    def __init__(self, capacity=1000):
        self.buffer = deque([''], maxlen=capacity)

    def write(self, content):
        lines = list(content.splitlines())
        self.buffer[0] += lines[0]
        if lines == ['']:
            # Bare newline
            self.buffer.appendleft('')
        else:
            self.buffer.extendleft(lines[1:])

    def getlines(self, start, stop=None):
        if stop:
            slice_ = islice(self.buffer, start, stop)
        else:
            slice_ = islice(self.buffer, start)
        return reversed(list(slice_))


class DummyStream:
    def write(self, content):
        pass


def wait_for_txs(client, txhashes, timeout=180):
    start = time.monotonic()
    outstanding = False
    txhashes = txhashes[:]
    while txhashes and time.monotonic() - start < timeout:
        remaining_timeout = timeout - (time.monotonic() - start)
        if outstanding != len(txhashes) or int(remaining_timeout) % 10 == 0:
            outstanding = len(txhashes)
            log.debug(
                "Waiting for tx confirmations",
                outstanding=outstanding,
                timeout_remaining=int(remaining_timeout),
            )
        for txhash in txhashes[:]:
            tx = client.web3.eth.getTransaction(txhash)
            if tx and tx['blockNumber'] is not None:
                txhashes.remove(txhash)
            time.sleep(.1)
        time.sleep(.5)
    if len(txhashes):
        raise ScenarioTxError(f"Timeout waiting for txhashes: {', '.join(txhashes)}")


def get_or_deploy_token(client: JSONRPCClient, scenario: dict) -> ContractProxy:
    """ Deploy or reuse  """
    token_contract = CONTRACT_MANAGER.get_contract(CONTRACT_CUSTOM_TOKEN)

    token_config = scenario.get('token', {})
    if not token_config:
        token_config = {}
    address = token_config.get('address')
    if address:
        check_address_has_code(client, address, 'Token')
        token_ctr = client.new_contract_proxy(token_contract['abi'], address)

        log.debug(
            "Reusing token",
            address=to_checksum_address(address),
            name=token_ctr.contract.functions.name().call(),
            symbol=token_ctr.contract.functions.symbol().call(),
        )
        return token_ctr

    token_id = uuid.uuid4()
    now = datetime.now()
    name = token_config.get('name', f"Scenario Test Token {token_id!s} {now:%Y-%m-%dT%H:%M}")
    symbol = token_config.get('symbol', f"T{token_id!s:.3}")

    log.debug("Deploying token", name=name, symbol=symbol)

    token_ctr = client.deploy_solidity_contract(
        'CustomToken',
        CONTRACT_MANAGER._contracts,
        constructor_parameters=(0, 0, name, symbol),
        confirmations=1,

    )
    log.info(
        "Deployed token",
        address=to_checksum_address(token_ctr.contract_address),
        name=name,
        symbol=symbol,
    )
    return token_ctr


def send_notification_mail(target_mail, subject, message, api_key):
    log.debug('Sending notification mail', subject=subject, message=message)
    res = requests.post(
        "https://api.mailgun.net/v3/notification.brainbot.com/messages",
        auth=("api", api_key),
        data={
            "from": "Raiden Scenario Player <scenario-player@notification.brainbot.com>",
            "to": [target_mail],
            "subject": subject,
            "text": message,
        },
    )
    log.debug('Notification mail result', code=res.status_code, text=res.text)


@contextmanager
def raw_input(stream=sys.stdin):
    original_stty = termios.tcgetattr(stream)
    original_fl = fcntl.fcntl(stream, fcntl.F_GETFL)
    tty.setcbreak(stream)
    fcntl.fcntl(stream, fcntl.F_SETFL, original_fl | os.O_NONBLOCK)
    yield
    fcntl.fcntl(stream, fcntl.F_SETFL, original_fl)
    termios.tcsetattr(stream, termios.TCSANOW, original_stty)


def get_gas_price_strategy(gas_price: Union[int, str]) -> callable:
    if isinstance(gas_price, int):
        def fixed_gas_price(web3, tx):
            return gas_price
        return fixed_gas_price
    elif gas_price == 'fast':
        return fast_gas_price_strategy
    elif gas_price == 'medium':
        return medium_gas_price_strategy
    else:
        raise ValueError(f'Invalid gas_price value: "{gas_price}"')
