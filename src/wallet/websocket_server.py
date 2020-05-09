import asyncio
import json
import logging
import signal
import time
import traceback
from pathlib import Path

from typing import Any, Dict, List, Optional, Tuple

import websockets

from src.types.peer_info import PeerInfo
from src.util.byte_types import hexstr_to_bytes
from src.util.keychain import Keychain, seed_from_mnemonic, generate_mnemonic
from src.util.path import path_from_root
from src.wallet.trade_manager import TradeManager
from src.wallet.util.json_util import dict_to_json_str

try:
    import uvloop
except ImportError:
    uvloop = None

from src.server.outbound_message import NodeType, OutboundMessage, Message, Delivery
from src.server.server import ChiaServer
from src.simulator.simulator_constants import test_constants
from src.simulator.simulator_protocol import FarmNewBlockProtocol
from src.util.config import load_config_cli, load_config
from src.util.ints import uint64
from src.types.sized_bytes import bytes32
from src.util.logging import initialize_logging
from src.wallet.util.wallet_types import WalletType
from src.wallet.rl_wallet.rl_wallet import RLWallet
from src.wallet.cc_wallet.cc_wallet import CCWallet
from src.wallet.wallet_info import WalletInfo
from src.wallet.wallet_node import WalletNode
from src.types.mempool_inclusion_status import MempoolInclusionStatus
from src.util.default_root import DEFAULT_ROOT_PATH
from src.util.setproctitle import setproctitle

# Timeout for response from wallet/full node for sending a transaction
TIMEOUT = 30

log = logging.getLogger(__name__)

def format_response(command: str, response_data: Dict[str, Any]):
    """
    Formats the response into standard format used between renderer.js and here
    """
    response = {"command": command, "data": response_data}

    json_str = dict_to_json_str(response)
    return json_str


class WebSocketServer:
    def __init__(self, keychain: Keychain, root_path: Path):
        self.config = load_config_cli(root_path, "config.yaml", "wallet")
        initialize_logging("Wallet %(name)-25s", self.config["logging"], root_path)
        self.log = log
        self.keychain = keychain
        self.websocket = None
        self.root_path = root_path
        self.wallet_node: Optional[WalletNode] = None
        self.trade_manager: Optional[TradeManager] = None
        if self.config["testing"] is True:
            self.config["database_path"] = "test_db_wallet.db"


    async def start(self):
        self.log.info("Starting Websocket Server")

        def master_close_cb():
            asyncio.ensure_future(self.stop())

        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGINT, master_close_cb)
            asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, master_close_cb)
        except NotImplementedError:
            self.log.info("Not implemented")

        private_key = self.keychain.get_wallet_key()
        if private_key is not None:
            await self.start_wallet()

        self.websocket_server = await websockets.serve(
            self.safe_handle, "localhost", self.config["rpc_port"]
        )
        self.log.info("Waiting webSocketServer closure")
        await self.websocket_server.wait_closed()
        self.log.info("webSocketServer closed")


    async def start_wallet(self) -> bool:
        private_key = self.keychain.get_wallet_key()
        if private_key is None:
            self.log.info("No keys")
            return False

        if self.config["testing"] is True:
            log.info(f"Websocket server in testing mode")
            self.wallet_node = await WalletNode.create(
                self.config, self.keychain, override_constants=test_constants
            )
        else:
            log.info(f"Not Testing")
            self.wallet_node = await WalletNode.create(self.config, self.keychain)

        self.trade_manager = await TradeManager.create(self.wallet_node.wallet_state_manager)
        self.wallet_node.wallet_state_manager.set_callback(self.state_changed_callback)

        net_config = load_config(self.root_path, "config.yaml")
        ping_interval = net_config.get("ping_interval")
        network_id = net_config.get("network_id")
        assert ping_interval is not None
        assert network_id is not None

        server = ChiaServer(
            self.config["port"],
            self.wallet_node,
            NodeType.WALLET,
            ping_interval,
            network_id,
            DEFAULT_ROOT_PATH,
            self.config,
        )
        self.wallet_node.set_server(server)

        if "full_node_peer" in self.config:
            full_node_peer = PeerInfo(
                self.config["full_node_peer"]["host"], self.config["full_node_peer"]["port"]
            )

            self.log.info(f"Connecting to full node peer at {full_node_peer}")
            server.global_connections.peers.add(full_node_peer)
            _ = await server.start_client(full_node_peer, None)

        if self.config["testing"] is False:
            self.wallet_node._start_bg_tasks()

        return True

    async def stop(self):
        self.websocket_server.close()
        if self.wallet_node is not None:
            self.wallet_node.server.close_all()
            await self.wallet_node.wallet_state_manager.close_all_stores()
            self.wallet_node._shutdown()

    async def get_next_puzzle_hash(self, websocket, request, response_api):
        """
        Returns a new puzzlehash
        """

        wallet_id = int(request["wallet_id"])
        wallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        puzzle_hash = (await wallet.get_new_puzzlehash()).hex()

        data = {
            "wallet_id": wallet_id,
            "puzzle_hash": puzzle_hash,
        }

        await websocket.send(format_response(response_api, data))

    async def send_transaction(self, websocket, request, response_api):
        wallet_id = int(request["wallet_id"])
        wallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        try:
            tx = await wallet.generate_signed_transaction_dict(request)
        except BaseException as e:
            data = {
                "status": "FAILED",
                "reason": f"Failed to generate signed transaction {e}",
            }
            return await websocket.send(format_response(response_api, data))

        if tx is None:
            data = {
                "status": "FAILED",
                "reason": "Failed to generate signed transaction",
            }
            return await websocket.send(format_response(response_api, data))
        try:
            await wallet.push_transaction(tx)
        except BaseException as e:
            data = {
                "status": "FAILED",
                "reason": f"Failed to push transaction {e}",
            }
            return await websocket.send(format_response(response_api, data))
        self.log.error(tx)
        sent = False
        start = time.time()
        while time.time() - start < TIMEOUT:
            sent_to: List[
                Tuple[str, MempoolInclusionStatus, Optional[str]]
            ] = await self.wallet_node.wallet_state_manager.get_transaction_status(
                tx.name()
            )

            if len(sent_to) == 0:
                await asyncio.sleep(0.1)
                continue
            status, err = sent_to[0][1], sent_to[0][2]
            if status == MempoolInclusionStatus.SUCCESS:
                data = {"status": "SUCCESS"}
                sent = True
                break
            elif status == MempoolInclusionStatus.PENDING:
                assert err is not None
                data = {"status": "PENDING", "reason": err}
                sent = True
                break
            elif status == MempoolInclusionStatus.FAILED:
                assert err is not None
                data = {"status": "FAILED", "reason": err}
                sent = True
                break
        if not sent:
            data = {
                "status": "FAILED",
                "reason": "Timed out. Transaction may or may not have been sent.",
            }

        return await websocket.send(format_response(response_api, data))

    async def server_ready(self, websocket, response_api):
        response = {"success": True}
        await websocket.send(format_response(response_api, response))

    async def get_transactions(self, websocket, request, response_api):
        wallet_id = int(request["wallet_id"])
        transactions = await self.wallet_node.wallet_state_manager.get_all_transactions(
            wallet_id
        )

        response = {"success": True, "txs": transactions, "wallet_id": wallet_id}
        await websocket.send(format_response(response_api, response))

    async def farm_block(self, websocket, request, response_api):
        puzzle_hash = bytes.fromhex(request["puzzle_hash"])
        request = FarmNewBlockProtocol(puzzle_hash)
        msg = OutboundMessage(
            NodeType.FULL_NODE, Message("farm_new_block", request), Delivery.BROADCAST,
        )

        self.wallet_node.server.push_message(msg)

    async def get_wallet_balance(self, websocket, request, response_api):
        wallet_id = int(request["wallet_id"])
        wallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        balance = await wallet.get_confirmed_balance()
        pending_balance = await wallet.get_unconfirmed_balance()

        response = {
            "wallet_id": wallet_id,
            "success": True,
            "confirmed_wallet_balance": balance,
            "unconfirmed_wallet_balance": pending_balance,
        }

        await websocket.send(format_response(response_api, response))

    async def get_sync_status(self, websocket, response_api):
        syncing = self.wallet_node.wallet_state_manager.sync_mode

        response = {"syncing": syncing}

        await websocket.send(format_response(response_api, response))

    async def get_height_info(self, websocket, response_api):
        lca = self.wallet_node.wallet_state_manager.lca
        height = self.wallet_node.wallet_state_manager.block_records[lca].height

        response = {"height": height}

        await websocket.send(format_response(response_api, response))

    async def get_connection_info(self, websocket, response_api):
        connections = (
            self.wallet_node.server.global_connections.get_full_node_peerinfos()
        )

        response = {"connections": connections}

        await websocket.send(format_response(response_api, response))

    async def create_new_wallet(self, websocket, request, response_api):
        config, key_config, wallet_state_manager, main_wallet = self.get_wallet_config()
        if request["wallet_type"] == "rl_wallet":
            if request["mode"] == "admin":
                rl_admin: RLWallet = await RLWallet.create_rl_admin(
                    config, key_config, wallet_state_manager, main_wallet
                )
                self.wallet_node.wallet_state_manager.wallets[
                    rl_admin.wallet_info.id
                ] = rl_admin
                response = {"success": True, "type": "rl_wallet"}
                return await websocket.send(format_response(response_api, response))
            elif request["mode"] == "user":
                rl_user: RLWallet = await RLWallet.create_rl_user(
                    config, key_config, wallet_state_manager, main_wallet
                )
                self.wallet_node.wallet_state_manager.wallets[
                    rl_user.wallet_info.id
                ] = rl_user
                response = {"success": True, "type": "rl_wallet"}
                return await websocket.send(format_response(response_api, response))
        elif request["wallet_type"] == "cc_wallet":
            if request["mode"] == "new":
                cc_wallet: CCWallet = await CCWallet.create_new_cc(
                    wallet_state_manager, main_wallet, request["amount"]
                )
                response = {"success": True, "type": cc_wallet.wallet_info.type.name}
                return await websocket.send(format_response(response_api, response))
            elif request["mode"] == "existing":
                cc_wallet = await CCWallet.create_wallet_for_cc(
                    wallet_state_manager, main_wallet, request["colour"]
                )
                response = {"success": True, "type": cc_wallet.wallet_info.type.name}
                return await websocket.send(format_response(response_api, response))

        response = {"success": False}
        return await websocket.send(format_response(response_api, response))

    def get_wallet_config(self):
        return (
            self.wallet_node.config,
            self.wallet_node.key_config,
            self.wallet_node.wallet_state_manager,
            self.wallet_node.wallet_state_manager.main_wallet,
        )

    async def get_wallets(self, websocket, response_api):
        wallets: List[
            WalletInfo
        ] = await self.wallet_node.wallet_state_manager.get_all_wallets()

        response = {"wallets": wallets}

        return await websocket.send(format_response(response_api, response))

    async def rl_set_admin_info(self, websocket, request, response_api):
        wallet_id = int(request["wallet_id"])
        wallet: RLWallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        user_pubkey = request["user_pubkey"]
        limit = uint64(int(request["limit"]))
        interval = uint64(int(request["interval"]))
        amount = uint64(int(request["amount"]))

        success = await wallet.admin_create_coin(interval, limit, user_pubkey, amount)

        response = {"success": success}

        return await websocket.send(format_response(response_api, response))

    async def rl_set_user_info(self, websocket, request, response_api):
        wallet_id = int(request["wallet_id"])
        wallet: RLWallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        admin_pubkey = request["admin_pubkey"]
        limit = uint64(int(request["limit"]))
        interval = uint64(int(request["interval"]))
        origin_id = request["origin_id"]

        success = await wallet.set_user_info(interval, limit, origin_id, admin_pubkey)

        response = {"success": success}

        return await websocket.send(format_response(response_api, response))

    async def cc_set_name(self, websocket, request, response_api):
        wallet_id = int(request["wallet_id"])
        wallet: CCWallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        success = await wallet.set_name(str(request["name"]))
        response = {"success": success}
        return await websocket.send(format_response(response_api, response))

    async def cc_get_name(self, websocket, request, response_api):
        wallet_id = int(request["wallet_id"])
        wallet: CCWallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        name: str = await wallet.get_name()
        response = {"name": name}
        return await websocket.send(format_response(response_api, response))

    async def cc_generate_zero_val(self, websocket, request, response_api):
        wallet_id = int(request["wallet_id"])
        wallet: CCWallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        try:
            tx = await wallet.generate_zero_val_coin()
        except BaseException as e:
            data = {
                "status": "FAILED",
                "reason": f"{e}",
            }
            return await websocket.send(format_response(response_api, data))

        if tx is None:
            data = {
                "status": "FAILED",
                "reason": "Failed to generate signed transaction",
            }
            return await websocket.send(format_response(response_api, data))
        self.log.error(tx)
        sent = False
        start = time.time()
        while time.time() - start < TIMEOUT:
            sent_to: List[
                Tuple[str, MempoolInclusionStatus, Optional[str]]
            ] = await self.wallet_node.wallet_state_manager.get_transaction_status(
                tx.name()
            )

            if len(sent_to) == 0:
                await asyncio.sleep(0.1)
                continue
            status, err = sent_to[0][1], sent_to[0][2]
            if status == MempoolInclusionStatus.SUCCESS:
                data = {"status": "SUCCESS"}
                sent = True
                break
            elif status == MempoolInclusionStatus.PENDING:
                assert err is not None
                data = {"status": "PENDING", "reason": err}
                sent = True
                break
            elif status == MempoolInclusionStatus.FAILED:
                assert err is not None
                data = {"status": "FAILED", "reason": err}
                sent = True
                break
        if not sent:
            data = {
                "status": "FAILED",
                "reason": "Timed out. Transaction may or may not have been sent.",
            }
        return await websocket.send(format_response(response_api, data))

    async def cc_spend(self, websocket, request, response_api):
        wallet_id = int(request["wallet_id"])
        wallet: CCWallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        puzzle_hash = hexstr_to_bytes(request["innerpuzhash"])
        try:
            tx = await wallet.cc_spend(request["amount"], puzzle_hash)
        except BaseException as e:
            data = {
                "status": "FAILED",
                "reason": f"{e}",
            }
            return await websocket.send(format_response(response_api, data))

        if tx is None:
            data = {
                "status": "FAILED",
                "reason": "Failed to generate signed transaction",
            }
            return await websocket.send(format_response(response_api, data))

        self.log.error(tx)
        sent = False
        start = time.time()
        while time.time() - start < TIMEOUT:
            sent_to: List[
                Tuple[str, MempoolInclusionStatus, Optional[str]]
            ] = await self.wallet_node.wallet_state_manager.get_transaction_status(
                tx.name()
            )

            if len(sent_to) == 0:
                await asyncio.sleep(0.1)
                continue
            status, err = sent_to[0][1], sent_to[0][2]
            if status == MempoolInclusionStatus.SUCCESS:
                data = {"status": "SUCCESS"}
                sent = True
                break
            elif status == MempoolInclusionStatus.PENDING:
                assert err is not None
                data = {"status": "PENDING", "reason": err}
                sent = True
                break
            elif status == MempoolInclusionStatus.FAILED:
                assert err is not None
                data = {"status": "FAILED", "reason": err}
                sent = True
                break
        if not sent:
            data = {
                "status": "FAILED",
                "reason": "Timed out. Transaction may or may not have been sent.",
            }

        return await websocket.send(format_response(response_api, data))

    async def cc_get_new_innerpuzzlehash(self, websocket, request, response_api):

        wallet_id = int(request["wallet_id"])
        wallet: CCWallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        innerpuz: bytes32 = await wallet.get_new_inner_hash()
        response = {"innerpuz": innerpuz.hex()}
        return await websocket.send(format_response(response_api, response))

    async def cc_get_colour(self, websocket, request, response_api):
        wallet_id = int(request["wallet_id"])
        wallet: CCWallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
        colour: str = await wallet.get_colour()
        response = {"colour": colour, "wallet_id": wallet_id}
        return await websocket.send(format_response(response_api, response))

    async def get_wallet_summaries(self, websocket, request, response_api):
        response = {}
        for wallet_id in self.wallet_node.wallet_state_manager.wallets:
            wallet = self.wallet_node.wallet_state_manager.wallets[wallet_id]
            balance = await wallet.get_confirmed_balance()
            type = wallet.wallet_info.type
            if type == WalletType.COLOURED_COIN:
                name = wallet.cc_info.my_colour_name
                colour = await wallet.get_colour()
                response[wallet_id] = {
                    "type": type,
                    "balance": balance,
                    "name": name,
                    "colour": colour,
                }
            else:
                response[wallet_id] = {"type": type, "balance": balance}
        return await websocket.send(format_response(response_api, response))

    async def get_discrepancies_for_offer(self, websocket, request, response_api):
        file_name = request["filename"]
        file_path = Path(file_name)
        (
            success,
            discrepancies,
            error,
        ) = await self.trade_manager.get_discrepancies_for_offer(file_path)

        if success:
            response = {"success": True, "discrepancies": discrepancies}
        else:
            response = {"success": False, "error": error}

        return await websocket.send(format_response(response_api, response))

    async def create_offer_for_ids(self, websocket, request, response_api):
        offer = request["ids"]
        file_name = request["filename"]
        success, spend_bundle, error = await self.trade_manager.create_offer_for_ids(
            offer
        )
        if success:
            self.trade_manager.write_offer_to_disk(Path(file_name), spend_bundle)
            response = {"success": success}
        else:
            response = {"success": success, "reason": error}

        return await websocket.send(format_response(response_api, response))

    async def respond_to_offer(self, websocket, request, response_api):
        file_path = Path(request["filename"])
        success, reason = await self.trade_manager.respond_to_offer(file_path)
        if success:
            response = {"success": success}
        else:
            response = {"success": success, "reason": reason}
        return await websocket.send(format_response(response_api, response))

    async def logged_in(self, websocket, response_api):
        private_key = self.keychain.get_wallet_key()
        if private_key is None:
            response = {"logged_in": False}
        else:
            response = {"logged_in": True}

        return await websocket.send(format_response(response_api, response))

    async def log_in(self, websocket, request, response_api):
        await self.stop_wallet()
        await self.clean_all_state()
        mnemonic = request["mnemonic"]
        self.log.info(f"Mnemonic {mnemonic}")
        seed = seed_from_mnemonic(mnemonic)
        self.log.info(f"Seed {seed}")
        self.keychain.set_wallet_seed(seed)
        k_seed = self.keychain.get_wallet_seed()

        await self.start_wallet()

        if k_seed == seed:
            response = {"success": True}
        else:
            response = {"success": False}

        return await websocket.send(format_response(response_api, response))

    async def clean_all_state(self):
        self.keychain.delete_all_keys()
        path = path_from_root(DEFAULT_ROOT_PATH, self.config["database_path"])
        if path.exists():
            path.unlink()

    async def stop_wallet(self):
        if self.wallet_node is not None:
            if self.wallet_node.server is not None:
                self.wallet_node.server.close_all()
            await self.wallet_node.wallet_state_manager.close_all_stores()
            self.wallet_node = None

    async def log_out(self, websocket, response_api):
        await self.stop_wallet()
        await self.clean_all_state()
        response = {"success": True}
        return await websocket.send(format_response(response_api, response))

    async def generate_mnemonic(self, websocket, response_api):
        mnemonic = generate_mnemonic()
        response = {"success": True, "mnemonic": mnemonic}
        return await websocket.send(format_response(response_api, response))

    async def safe_handle(self, websocket, path):
        async for message in websocket:
            command = None
            try:
                decoded = json.loads(message)
                command = decoded["command"]
                await self.handle_message(websocket, message, path)
            except (BaseException, websockets.exceptions.ConnectionClosedError) as e:
                if isinstance(e, websockets.exceptions.ConnectionClosedError):
                    tb = traceback.format_exc()
                    self.log.warning(f"ConnectionClosedError. Closing websocket. {tb}")
                    await websocket.close()
                else:
                    tb = traceback.format_exc()
                    self.log.error(f"Error while handling message: {tb}")
                    error = {"success": False, "error": f"{e}" }
                    if command is None:
                        command = "UnknownCommand"
                    await websocket.send(format_response(command, error))

    async def handle_message(self, websocket, message, path):
        """
        This function gets called when new message is received via websocket.
        """

        decoded = json.loads(message)
        self.log.info(f"decoded: {decoded}")
        command = decoded["command"]
        data = None
        if "data" in decoded:
            data = decoded["data"]
        if command == "start_server":
            self.websocket = websocket
            await self.server_ready(websocket, command)
        elif command == "get_wallet_balance":
            await self.get_wallet_balance(websocket, data, command)
        elif command == "send_transaction":
            await self.send_transaction(websocket, data, command)
        elif command == "get_next_puzzle_hash":
            await self.get_next_puzzle_hash(websocket, data, command)
        elif command == "get_transactions":
            await self.get_transactions(websocket, data, command)
        elif command == "farm_block":
            await self.farm_block(websocket, data, command)
        elif command == "get_sync_status":
            await self.get_sync_status(websocket, command)
        elif command == "get_height_info":
            await self.get_height_info(websocket, command)
        elif command == "get_connection_info":
            await self.get_connection_info(websocket, command)
        elif command == "create_new_wallet":
            await self.create_new_wallet(websocket, data, command)
        elif command == "get_wallets":
            await self.get_wallets(websocket, command)
        elif command == "rl_set_admin_info":
            await self.rl_set_admin_info(websocket, data, command)
        elif command == "rl_set_user_info":
            await self.rl_set_user_info(websocket, data, command)
        elif command == "cc_set_name":
            await self.cc_set_name(websocket, data, command)
        elif command == "cc_get_name":
            await self.cc_get_name(websocket, data, command)
        elif command == "cc_generate_zero_val":
            await self.cc_generate_zero_val(websocket, data, command)
        elif command == "cc_spend":
            await self.cc_spend(websocket, data, command)
        elif command == "cc_get_innerpuzzlehash":
            await self.cc_get_new_innerpuzzlehash(websocket, data, command)
        elif command == "cc_get_colour":
            await self.cc_get_colour(websocket, data, command)
        elif command == "create_offer":
            await self.create_offer_for_colours(websocket, data, command)
        elif command == "create_offer_for_ids":
            await self.create_offer_for_ids(websocket, data, command)
        elif command == "get_discrepancies_for_offer":
            await self.get_discrepancies_for_offer(websocket, data, command)
        elif command == "respond_to_offer":
            await self.respond_to_offer(websocket, data, command)
        elif command == "get_wallet_summaries":
            await self.get_wallet_summaries(websocket, data, command)
        elif command == "logged_in":
            await self.logged_in(websocket, command)
        elif command == "generate_mnemonic":
            await self.generate_mnemonic(websocket, command)
        elif command == "log_in":
            await self.log_in(websocket, data, command)
        elif command == "log_out":
            await self.log_out(websocket, command)
        else:
            response = {"error": f"unknown_command {command}"}
            await websocket.send(dict_to_json_str(response))

    async def notify_ui_that_state_changed(self, state: str):
        data = {
            "state": state,
        }
        if self.websocket is not None:
            try:
                await self.websocket.send(format_response("state_changed", data))
            except (BaseException, websockets.exceptions.ConnectionClosedError) as e:
                try:
                    self.log.warning(f"Caught exception {type(e)}, closing websocket")
                    await self.websocket.close()
                except BrokenPipeError:
                    pass
                finally:
                    self.websocket = None

    def state_changed_callback(self, state: str):
        if self.websocket is None:
            return
        asyncio.create_task(self.notify_ui_that_state_changed(state))


async def start_websocket_server():
    """
    Starts WalletNode, WebSocketServer, and ChiaServer
    """


    setproctitle("chia-wallet")
    keychain = Keychain.create(testing=False)
    websocket_server = WebSocketServer(keychain, DEFAULT_ROOT_PATH)
    await websocket_server.start()
    log.info("Wallet fully closed")


def main():
    if uvloop is not None:
        uvloop.install()
    asyncio.run(start_websocket_server())


if __name__ == "__main__":
    try:
        main()
    except Exception:
        tb = traceback.format_exc()
        log = logging.getLogger(__name__)
        log.error(f"Error in wallet. {tb}")
        raise
