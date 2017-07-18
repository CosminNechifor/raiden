import json
import logging
import traceback
from datetime import datetime
from os.path import basename

import click
import gevent
import structlog
from blessings import Terminal
from eth_utils import to_checksum_address
from web3.utils.transactions import TRANSACTION_DEFAULTS

from raiden.accounts import Account
from raiden.log_config import configure_logging
from scenario_player.runner import ScenarioRunner
from scenario_player.tasks import TaskState
from scenario_player.utils import DummyStream, LogBuffer, send_notification_mail

log = structlog.get_logger(__name__)

TRANSACTION_DEFAULTS['gas'] = lambda web3, tx: web3.eth.estimateGas(tx) * 2


def _ui(term, runner, log_file_name, log_buffer):
    last_height = 0
    last_width = 0
    separator = 1
    scrollback = 0
    while True:
        size_changed = (
            term.height != last_height or
            term.width != last_width or (
                runner.root_task and separator == 1
            )
        )
        if size_changed:
            print(term.clear, end='')
            last_height = term.height
            last_width = term.width
            if runner.root_task:
                separator = int((term.height - 1) / 3) * 2

        bar_color = term.blue
        if runner.root_task:
            if runner.root_task.state is TaskState.FINISHED:
                bar_color = term.green
            elif runner.root_task.state is TaskState.ERRORED:
                bar_color = term.red

        with term.location(0, 0):
            print(f'{bar_color}{" TASKS ":=^{term.width}}{term.normal}')
            if runner.root_task:
                lines = [
                    f'{line}{term.clear_eol}'
                    for line in str(runner.root_task).splitlines()
                ]
                print('\n'.join(lines[:separator]))

        with term.location(0, separator):
            print(f'{bar_color}{" LOG ":=^{term.width}}{term.normal}')
            line_count = term.height - separator - 1
            log_lines = log_buffer.getlines(scrollback, line_count + scrollback)
            for i, line in enumerate(log_lines, start=1):
                end = '\n' if i < line_count else ''
                print(line + term.clear_eol, end=end)

        with term.location(0, term.height - 1):
            msg = (
                f'Nodes: {len(runner.raiden_nodes)} - '
                f'Tasks: {runner.task_count} - '
                f'Running: {runner.running_task_count} - '
                f'Logfile: {log_file_name}'
            )
            print(
                f'{term.white_on_blue}{msg}{term.clear_eol}{term.normal}',
                end='',
            )
        gevent.sleep(.125)


@click.command()
@click.option("--keystore-file", required=True, type=click.Path(exists=True, dir_okay=False))
@click.password_option("--password", envvar="ACCOUNT_PASSWORD", required=True)
@click.option("--rpc-url", default="http://localhost:8545")
@click.option("--auth", default="")
@click.option("--mailgun-api-key")
@click.argument("scenario-file", type=click.File())
def main(scenario_file, keystore_file, password, rpc_url, auth, mailgun_api_key):
    gevent.get_hub().exception_stream = DummyStream()
    scenario_basename = basename(scenario_file.name)
    log_file_name = f'scenario-player_{scenario_basename}_{datetime.now():%Y-%m-%dT%H:%M:%S}.log'
    click.secho(f'Writing log to {log_file_name}', fg='yellow')
    configure_logging(
        {'': 'INFO', 'raiden': 'DEBUG', 'scenario_player': 'DEBUG'},
        debug_log_file_name=log_file_name,
        _first_party_packages=frozenset(['raiden', 'scenario_player']),
    )
    log_buffer = LogBuffer()
    for handler in logging.getLogger('').handlers:
        if isinstance(handler, logging.StreamHandler):
            handler.stream = log_buffer
            break
    with open(keystore_file, 'r') as keystore:
        account = Account(json.load(keystore), password, keystore_file)
        log.info("Using account", account=to_checksum_address(account.address))
    runner = ScenarioRunner(account, rpc_url, auth, scenario_file)
    terminal = Terminal()
    # Disable line wrapping
    print(terminal.rmam, end='')
    gevent.spawn(_ui, terminal, runner, log_file_name, log_buffer)
    try:
        assert_errors = runner.run_scenario()
        if assert_errors:
            log.error('Run finished', result='assertion errors')
        else:
            log.info('Run finished', result='success')
        if runner.notification_email:
            if not mailgun_api_key:
                log.error("Can't send notification mail. No API key provided")
                return 1
            log.info('Sending notification mail')
            if assert_errors:
                send_notification_mail(
                    runner.notification_email,
                    f'Unexpected channel balances in {scenario_file.name}',
                    json.dumps(assert_errors),
                    mailgun_api_key,
                )
            else:
                send_notification_mail(
                    runner.notification_email,
                    f'Scenario successful {scenario_file.name}',
                    'Success',
                    mailgun_api_key,
                )
    except Exception:
        if runner.notification_email and mailgun_api_key:
            send_notification_mail(
                runner.notification_email,
                f'Error running scenario {scenario_file.name}',
                traceback.format_exc(),
                mailgun_api_key,
            )
        log.exception('Exception while running scenario')
    finally:
        if terminal.is_a_tty:
            log.warning('Press Ctrl-C to exit')
            while True:
                gevent.sleep(1)
        # Re-enable line wrapping
        print(terminal.smam, end='')


# Old code maybe useful for 'spare parts':

# if scenario.get('connect', False):
#     active_nodes = []
#     for node in raiden_nodes:
#         url = API_URL_CONNECT.format(node, token_address)
#         log.info("Connecting to token network", node=node, token=token_address, url=url)
#         try:
#             resp = requests.put(url, json=dict(funds=TOKEN_BALANCE_MIN / 2), timeout=timeout)
#             code = resp.status_code
#             msg = resp.text
#             active_nodes.append(node)
#         except RequestException as ex:
#             code = -1
#             msg = str(ex)
#         if not 199 < code < 300:
#             log.error("Couldn't join token network: %d %s", code, msg)
#
#     log.info("Active nodes", nodes=active_nodes)
#     raiden_nodes = active_nodes
#
#     excpected_amounts = defaultdict(int)
#
#     delay_min = scenario.get('delay_min', 2)
#     delay_max = scenario.get('delay_min', 20)
#
#     iterations = scenario.get('interations', 0)
#     i = 0
#
#     while True:
#         for node in raiden_nodes:
#             partner_node = None
#             while partner_node is None or partner_node == node:
#                 partner_node = random.choice(raiden_nodes)
#             partner_address = node_to_address[partner_node]
#
#             url = API_URL_TRANSFERS.format(node, token_address, partner_address)
#             amount = random.randint(1, 5)
#             log.info("Transfering", from_=node, to=partner_node, amount=amount)
#             excpected_amounts[partner_node] += amount
#             try:
#                 resp = requests.post(url, json=dict(amount=amount), timeout=timeout)
#                 code = resp.status_code
#                 msg = resp.text
#             except RequestException as ex:
#                 code = -1
#                 msg = str(ex)
#             if not 199 < code < 300:
#                 log.error("Couldn't transfer: %d %s", code, msg)
#                 continue
#
#             time.sleep(random.randint(delay_min, delay_max))
#         i += 1
#         if i >= iterations:
#             break
#
#     log.info("Expected transfers", amounts=dict(excpected_amounts))


if __name__ == "__main__":
    main()
