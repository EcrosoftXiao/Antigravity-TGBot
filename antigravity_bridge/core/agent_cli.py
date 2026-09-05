"""Subprocess wrapper for local Antigravity Agent agentapi CLI."""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import AVAILABLE_MODELS, ConversationInfo, ModelOption, update_available_models

logger = logging.getLogger(__name__)


class AgentCliBridge:
    """Provides high-level async methods to interact with local Antigravity Agent."""

    def __init__(
        self,
        agentapi_path: Optional[str] = None,
        gemini_dir: Optional[str] = None,
    ):
        self.gemini_dir = Path(
            gemini_dir or os.path.expanduser("~/.gemini/antigravity")
        ).resolve()
        self.agentapi_cmd = self._resolve_agentapi(agentapi_path)
        self._cached_ls_address: Optional[str] = os.getenv("ANTIGRAVITY_LS_ADDRESS")
        self._cached_csrf_token: Optional[str] = os.getenv("ANTIGRAVITY_CSRF_TOKEN")
        self._cached_models: List[ModelOption] = []
        self._models_cached_at: float = 0.0
        logger.info(f"Initialized AgentCliBridge using command: {self.agentapi_cmd}")

    def _resolve_agentapi(self, custom_path: Optional[str]) -> List[str]:
        """Find the executable command for agentapi."""
        if custom_path and os.path.isfile(custom_path):
            return [custom_path]

        # 1. ~/.gemini/antigravity/bin/agentapi
        default_bin = self.gemini_dir / "bin" / "agentapi"
        if default_bin.is_file() and os.access(default_bin, os.X_OK):
            return [str(default_bin)]

        # 2. macOS App bundle language_server agentapi
        app_lang_server = Path(
            "/Applications/Antigravity.app/Contents/Resources/bin/language_server"
        )
        if app_lang_server.is_file() and os.access(app_lang_server, os.X_OK):
            return [str(app_lang_server), "agentapi"]

        # 3. PATH discovery
        which_path = shutil.which("agentapi")
        if which_path:
            return [which_path]

        # Fallback to default path even if not yet verified
        return [str(default_bin)]

    def _discover_antigravity_env(self) -> Tuple[Optional[str], Optional[str]]:
        """Automatically detect running Antigravity Language Server address and CSRF token."""
        try:
            ps_proc = subprocess.run(
                ["ps", "-A", "-o", "pid,command"],
                capture_output=True,
                text=True,
                check=True,
                timeout=5,
            )
        except Exception as e:
            logger.debug(f"Failed to inspect processes: {e}")
            return None, None

        target_pid = None
        csrf_token = None
        host_bridge_port = None

        for line in ps_proc.stdout.splitlines():
            if "language_server" in line and "--csrf_token" in line:
                parts = line.strip().split(None, 1)
                target_pid = parts[0]
                m_csrf = re.search(r"--csrf_token\s+([a-zA-Z0-9-]+)", line)
                if m_csrf:
                    csrf_token = m_csrf.group(1)
                m_bridge = re.search(r"--host_bridge_url=http://127\.0\.0\.1:(\d+)", line)
                if m_bridge:
                    host_bridge_port = int(m_bridge.group(1))
                break

        if not target_pid:
            return None, None

        candidate_ports: List[int] = []
        if host_bridge_port:
            candidate_ports.extend([host_bridge_port + 2, host_bridge_port + 1, host_bridge_port])

        try:
            lsof_proc = subprocess.run(
                ["lsof", "-a", "-p", target_pid, "-iTCP", "-sTCP:LISTEN", "-P", "-n"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            for port_str in re.findall(r":(\d+)\s+\(LISTEN\)", lsof_proc.stdout):
                p = int(port_str)
                if p not in candidate_ports:
                    candidate_ports.append(p)
        except Exception:
            pass

        for port in candidate_ports:
            url = f"http://127.0.0.1:{port}/"
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "antigravity-bridge"})
                with urllib.request.urlopen(req, timeout=0.8) as resp:
                    content = resp.read().decode("utf-8", errors="ignore")
                    if "antigravity" in content and "csrfToken" in content:
                        m = re.search(r'"csrfToken":"([^"]+)"', content)
                        found_csrf = m.group(1) if m else csrf_token
                        logger.info(f"Discovered Antigravity Language Server at 127.0.0.1:{port}")
                        return f"127.0.0.1:{port}", found_csrf
            except Exception:
                continue

        if candidate_ports and csrf_token:
            return f"127.0.0.1:{candidate_ports[0]}", csrf_token

        return None, csrf_token

    def ensure_connection(self) -> str:
        """Verify or auto-discover connection parameters to Antigravity Language Server."""
        if not self._cached_ls_address or not self._cached_csrf_token:
            addr, csrf = self._discover_antigravity_env()
            if addr:
                self._cached_ls_address = addr
            if csrf:
                self._cached_csrf_token = csrf

        if not self._cached_ls_address:
            raise RuntimeError(
                "未检测到本地 Antigravity 宿主服务地址 (ANTIGRAVITY_LS_ADDRESS)！\n"
                "请确认：\n"
                "1. 本地 Antigravity 客户端 / IDE 是否已正常启动并处于运行状态。\n"
                "2. 或在项目 .env 中手动配置 ANTIGRAVITY_LS_ADDRESS 与 ANTIGRAVITY_CSRF_TOKEN。"
            )
        return self._cached_ls_address

    def _find_or_create_project_id_for_dir(self, cwd: str) -> Optional[str]:
        """Resolve or automatically register Antigravity project ID for a directory."""
        projects_dir = self.gemini_dir.parent / "config" / "projects"
        if not projects_dir.is_dir():
            projects_dir.mkdir(parents=True, exist_ok=True)

        abs_cwd = os.path.abspath(cwd)
        matches: List[Tuple[int, str]] = []
        fallback: Optional[str] = None

        for p in projects_dir.glob("*.json"):
            try:
                with open(p, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    p_id = data.get("id")
                    if not fallback and p_id:
                        fallback = p_id
                    for res in data.get("projectResources", {}).get("resources", []):
                        f_uri = res.get("gitFolder", {}).get("folderUri", "")
                        if f_uri.startswith("file://"):
                            f_path = os.path.abspath(f_uri[7:])
                            if abs_cwd == f_path or abs_cwd.startswith(f_path + "/"):
                                matches.append((len(f_path), p_id))
            except Exception:
                continue

        if matches:
            matches.sort(key=lambda x: x[0], reverse=True)
            return matches[0][1]

        # Auto-register new project config if directory not known to Antigravity
        try:
            import uuid
            new_pid = str(uuid.uuid4())
            new_pfile = projects_dir / f"{new_pid}.json"
            proj_name = os.path.basename(abs_cwd) or "workspace"
            config = {
                "id": new_pid,
                "name": proj_name,
                "projectResources": {
                    "resources": [
                        {
                            "gitFolder": {
                                "folderUri": f"file://{abs_cwd}",
                                "defaultBranch": "main",
                            }
                        }
                    ]
                },
                "settings": {
                    "fileAccessPolicy": "AGENT_SETTING_POLICY_ALLOW",
                    "sandboxMode": False,
                    "autoExecutionPolicy": "CASCADE_COMMANDS_AUTO_EXECUTION_EAGER",
                },
                "isWorkspaceOnly": False,
            }
            with open(new_pfile, "w", encoding="utf-8") as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            logger.info(f"Auto-registered Antigravity project '{proj_name}' ({new_pid}) for {abs_cwd}")
            return new_pid
        except Exception as exc:
            logger.warning(f"Failed to auto-register project config for {abs_cwd}: {exc}")
            return fallback

    def _get_clean_env(self, cwd: Optional[str] = None) -> Dict[str, str]:
        """Return environment variables with active Antigravity Language Server connection."""
        env = dict(os.environ)
        # Strip parent conversation context so new sessions run independently
        env.pop("ANTIGRAVITY_CONVERSATION_ID", None)
        env.pop("ANTIGRAVITY_SOURCE_METADATA", None)

        # Populate LS_ADDRESS and CSRF_TOKEN dynamically if missing
        if not env.get("ANTIGRAVITY_LS_ADDRESS") or not env.get("ANTIGRAVITY_CSRF_TOKEN"):
            if not self._cached_ls_address or not self._cached_csrf_token:
                addr, csrf = self._discover_antigravity_env()
                if addr:
                    self._cached_ls_address = addr
                if csrf:
                    self._cached_csrf_token = csrf

            if self._cached_ls_address:
                env.setdefault("ANTIGRAVITY_LS_ADDRESS", self._cached_ls_address)
            if self._cached_csrf_token:
                env.setdefault("ANTIGRAVITY_CSRF_TOKEN", self._cached_csrf_token)

        # Always set ANTIGRAVITY_PROJECT_ID specifically for target_cwd
        target_dir = cwd or os.getcwd()
        resolved_pid = self._find_or_create_project_id_for_dir(target_dir)
        if resolved_pid:
            env["ANTIGRAVITY_PROJECT_ID"] = resolved_pid

        if not env.get("ANTIGRAVITY_LS_ADDRESS"):
            raise RuntimeError(
                "未检测到本地 Antigravity 宿主服务地址 (ANTIGRAVITY_LS_ADDRESS)！\n"
                "请确认：\n"
                "1. 本地 Antigravity 客户端 / IDE 是否已正常启动并处于运行状态。\n"
                "2. 或在项目 .env 中手动配置 ANTIGRAVITY_LS_ADDRESS 与 ANTIGRAVITY_CSRF_TOKEN。"
            )

        return env

    async def _exec_agentapi(self, args: List[str], cwd: Optional[str] = None) -> Dict[str, Any]:
        """Execute agentapi command asynchronously and parse JSON output."""
        target_cwd = cwd or os.getcwd()
        cmd = self.agentapi_cmd + args
        logger.debug(f"Executing agentapi command: {' '.join(cmd)}")

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=self._get_clean_env(cwd=target_cwd),
            cwd=target_cwd,
        )
        stdout, stderr = await proc.communicate()

        stdout_str = stdout.decode("utf-8", errors="replace").strip()
        stderr_str = stderr.decode("utf-8", errors="replace").strip()

        if proc.returncode != 0:
            logger.error(
                f"agentapi exited with code {proc.returncode}. Stderr: {stderr_str}"
            )
            raise RuntimeError(
                f"agentapi error (code {proc.returncode}): {stderr_str or stdout_str}"
            )

        try:
            return json.loads(stdout_str)
        except json.JSONDecodeError as exc:
            logger.error(f"Failed to parse agentapi JSON output: {stdout_str}")
            raise RuntimeError(
                f"Invalid JSON returned by agentapi: {stdout_str}"
            ) from exc

    async def new_conversation(
        self,
        prompt: str,
        model: Optional[str] = None,
        title: Optional[str] = None,
        profile: Optional[str] = None,
        cwd: Optional[str] = None,
    ) -> str:
        """Create a new Antigravity agent conversation.
        
        Returns:
            conversation_id (str)
        """
        args = ["new-conversation"]
        if model:
            from .models import get_model_by_identifier
            opt = get_model_by_identifier(model)
            tier = opt.tier if opt else model
            args.append(f"--model={tier}")
        if title:
            args.append(f"--title={title}")
        if profile:
            args.append(f"--profile={profile}")
        args.append(prompt)

        data = await self._exec_agentapi(args, cwd=cwd)
        try:
            conv_id = data["response"]["newConversation"]["conversationId"]
            logger.info(f"Created new conversation: {conv_id}")
            return conv_id
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"Unexpected new-conversation response format: {data}") from exc

    async def send_message(
        self,
        conversation_id: str,
        content: str,
        title: Optional[str] = None,
    ) -> bool:
        """Send a message to an existing Antigravity conversation."""
        args = ["send-message"]
        if title:
            args.append(f"--title={title}")
        args.append(conversation_id)
        args.append(content)

        data = await self._exec_agentapi(args)
        logger.info(f"Dispatched message to conversation {conversation_id}")
        return "response" in data

    async def cancel_cascade(self, conversation_id: str) -> bool:
        """Send CancelCascadeInvocation RPC to Antigravity Language Server to halt an active task."""
        try:
            env = self._get_clean_env()
            ls_addr = env.get("ANTIGRAVITY_LS_ADDRESS")
            csrf_token = env.get("ANTIGRAVITY_CSRF_TOKEN")
            if not ls_addr:
                logger.warning("Cannot cancel cascade: ANTIGRAVITY_LS_ADDRESS not detected.")
                return False

            if not ls_addr.startswith("http://") and not ls_addr.startswith("https://"):
                url = f"http://{ls_addr}/exa.language_server_pb.LanguageServerService/CancelCascadeInvocation"
            else:
                url = f"{ls_addr}/exa.language_server_pb.LanguageServerService/CancelCascadeInvocation"

            headers = {
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
            }
            if csrf_token:
                headers["x-codeium-csrf-token"] = csrf_token

            payload = json.dumps({
                "cascade_id": conversation_id,
                "kill_background_tasks": True,
            }).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")

            loop = asyncio.get_running_loop()

            def _send_cancel() -> bool:
                try:
                    with urllib.request.urlopen(req, timeout=3) as resp:
                        return resp.status in (200, 204)
                except urllib.error.HTTPError as he:
                    body = he.read().decode("utf-8", errors="ignore")
                    logger.info(f"CancelCascadeInvocation returned HTTP {he.code}: {body}")
                    return he.code in (200, 204)
                except Exception as exc:
                    logger.warning(f"Failed calling CancelCascadeInvocation: {exc}")
                    return False

            success = await loop.run_in_executor(None, _send_cancel)
            logger.info(f"Dispatched CancelCascadeInvocation for conversation {conversation_id} (result={success})")
            return success
        except Exception as exc:
            logger.exception(f"Exception cancelling cascade for {conversation_id}: {exc}")
            return False

    async def get_cascade_trajectory(self, conversation_id: str) -> Optional[Dict[str, Any]]:
        """Fetch current trajectory for a cascade from Language Server."""
        try:
            env = self._get_clean_env()
            ls_addr = env.get("ANTIGRAVITY_LS_ADDRESS")
            csrf_token = env.get("ANTIGRAVITY_CSRF_TOKEN")
            if not ls_addr:
                return None

            if not ls_addr.startswith("http://") and not ls_addr.startswith("https://"):
                url = f"http://{ls_addr}/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory"
            else:
                url = f"{ls_addr}/exa.language_server_pb.LanguageServerService/GetCascadeTrajectory"

            headers = {
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
            }
            if csrf_token:
                headers["x-codeium-csrf-token"] = csrf_token

            payload = json.dumps({"cascade_id": conversation_id}).encode("utf-8")
            req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
            loop = asyncio.get_running_loop()

            def _fetch():
                try:
                    with urllib.request.urlopen(req, timeout=5) as resp:
                        if resp.status == 200:
                            return json.loads(resp.read().decode("utf-8", errors="ignore"))
                except Exception as e:
                    logger.debug(f"Failed to fetch cascade trajectory: {e}")
                return None

            return await loop.run_in_executor(None, _fetch)
        except Exception as exc:
            logger.debug(f"Exception in get_cascade_trajectory: {exc}")
            return None

    async def handle_ask_question_interaction(
        self,
        conversation_id: str,
        step_index: int,
        responses: Optional[List[Dict[str, Any]]] = None,
        cancelled: bool = False,
    ) -> bool:
        """Send HandleCascadeUserInteraction RPC to Antigravity Language Server to answer an interactive question."""
        try:
            env = self._get_clean_env()
            ls_addr = env.get("ANTIGRAVITY_LS_ADDRESS")
            csrf_token = env.get("ANTIGRAVITY_CSRF_TOKEN")
            if not ls_addr:
                logger.warning("Cannot handle user interaction: ANTIGRAVITY_LS_ADDRESS not detected.")
                return False

            if not ls_addr.startswith("http://") and not ls_addr.startswith("https://"):
                url = f"http://{ls_addr}/exa.language_server_pb.LanguageServerService/HandleCascadeUserInteraction"
            else:
                url = f"{ls_addr}/exa.language_server_pb.LanguageServerService/HandleCascadeUserInteraction"

            headers = {
                "Content-Type": "application/json",
                "Connect-Protocol-Version": "1",
            }
            if csrf_token:
                headers["x-codeium-csrf-token"] = csrf_token

            # 1. Dynamically discover the active trajectory_id and step_index from Language Server
            target_trajectory_id: Optional[str] = None
            target_step_index: Optional[int] = None

            traj_data = await self.get_cascade_trajectory(conversation_id)
            if traj_data and "trajectory" in traj_data:
                traj = traj_data["trajectory"]
                target_trajectory_id = traj.get("trajectoryId")
                steps = traj.get("steps", [])
                for s in reversed(steps):
                    if s.get("type") in ("CORTEX_STEP_TYPE_ASK_QUESTION", 3) or "askQuestion" in s:
                        s_info = s.get("metadata", {}).get("sourceTrajectoryStepInfo", {}) if s.get("metadata") else {}
                        step_idx_val = (s_info.get("stepIndex") if s_info else None) or s.get("stepIndex")
                        if step_idx_val is not None:
                            target_step_index = step_idx_val
                            target_trajectory_id = (s_info.get("trajectoryId") if s_info else None) or traj.get("trajectoryId") or target_trajectory_id
                            if s.get("status") != "CORTEX_STEP_STATUS_DONE":
                                break

            candidate_trajs: List[str] = []
            if target_trajectory_id:
                candidate_trajs.append(target_trajectory_id)
            if conversation_id not in candidate_trajs:
                candidate_trajs.append(conversation_id)

            candidate_steps: List[int] = []
            if target_step_index is not None:
                candidate_steps.append(target_step_index)
            # Typically ask_question tool step is step_index + 1 if step_index is the PLANNER_RESPONSE
            if step_index >= 0:
                if (step_index + 1) not in candidate_steps:
                    candidate_steps.append(step_index + 1)
                if step_index not in candidate_steps:
                    candidate_steps.append(step_index)
            if 0 not in candidate_steps:
                candidate_steps.append(0)

            logger.info(
                f"handle_ask_question_interaction: targeting conv {conversation_id[:8]} "
                f"with candidate trajs: {candidate_trajs}, candidate steps: {candidate_steps}"
            )

            loop = asyncio.get_running_loop()

            for t_id in candidate_trajs:
                for s_idx in candidate_steps:
                    interaction_payload: Dict[str, Any] = {
                        "trajectory_id": t_id,
                        "step_index": s_idx,
                        "ask_question": {
                            "cancelled": cancelled,
                        },
                    }
                    if responses is not None:
                        interaction_payload["ask_question"]["responses"] = responses

                    payload_data = json.dumps({
                        "cascade_id": conversation_id,
                        "interaction": interaction_payload,
                    }).encode("utf-8")

                    req = urllib.request.Request(url, data=payload_data, headers=headers, method="POST")

                    def _send_rpc(req=req, t_id=t_id, s_idx=s_idx) -> Tuple[bool, Optional[str]]:
                        try:
                            with urllib.request.urlopen(req, timeout=5) as resp:
                                body = resp.read().decode("utf-8", errors="ignore")
                                logger.info(
                                    f"HandleCascadeUserInteraction success ({resp.status}) for conv {conversation_id[:8]} "
                                    f"(traj {t_id[:8]}, step {s_idx})"
                                )
                                return (resp.status in (200, 204), None)
                        except urllib.error.HTTPError as he:
                            body = he.read().decode("utf-8", errors="ignore")
                            return (False, body)
                        except Exception as exc:
                            return (False, str(exc))

                    success, err_msg = await loop.run_in_executor(None, _send_rpc)
                    if success:
                        return True
                    else:
                        logger.debug(
                            f"HandleCascadeUserInteraction failed for traj {t_id[:8]} step {s_idx}: {err_msg}"
                        )
                        if err_msg and "input not registered for step" not in err_msg:
                            logger.warning(f"HandleCascadeUserInteraction error: {err_msg}")

            return False
        except Exception as exc:
            logger.exception(f"Exception in handle_ask_question_interaction for {conversation_id}: {exc}")
            return False

    async def get_metadata(self, conversation_id: str) -> Dict[str, Any]:
        """Fetch metadata for a conversation."""
        args = ["get-conversation-metadata", conversation_id]
        return await self._exec_agentapi(args)

    async def list_conversations(self, limit: int = 20) -> List[ConversationInfo]:
        """List local Antigravity conversations sorted from newest to oldest."""
        brain_dir = self.gemini_dir / "brain"
        if not brain_dir.is_dir():
            return []

        conv_list: List[ConversationInfo] = []
        entries = []

        # Find all conversation directories in brain/
        for p in brain_dir.iterdir():
            if p.is_dir() and not p.name.startswith("."):
                try:
                    mtime = p.stat().st_mtime
                    entries.append((mtime, p))
                except OSError:
                    continue

        # Sort by latest mtime
        entries.sort(key=lambda x: x[0], reverse=True)

        for mtime, p in entries[: limit * 2]:
            conv_id = p.name
            transcript_file = p / ".system_generated" / "logs" / "transcript.jsonl"
            first_prompt = ""
            created_at = ""

            if transcript_file.is_file():
                try:
                    with open(transcript_file, "r", encoding="utf-8", errors="replace") as f:
                        first_line = f.readline()
                        if first_line:
                            data = json.loads(first_line)
                            created_at = data.get("created_at", "")
                            raw_content = data.get("content", "")
                            # Strip XML user tags if present
                            clean_prompt = re.sub(r"<USER_REQUEST>|\n?</USER_REQUEST>", "", raw_content).strip()
                            clean_prompt = re.sub(r"<ADDITIONAL_METADATA>[\s\S]*?</ADDITIONAL_METADATA>", "", clean_prompt).strip()
                            first_prompt = clean_prompt.split("\n")[0][:100]
                except Exception:
                    pass

            if not first_prompt:
                first_prompt = "(No initial prompt)"

            conv_list.append(
                ConversationInfo(
                    conversation_id=conv_id,
                    created_at=created_at or str(mtime),
                    title=first_prompt[:60],
                    first_prompt=first_prompt,
                )
            )

            if len(conv_list) >= limit:
                break

        return conv_list

    async def get_conversation_history(
        self, conversation_id: str, limit: int = 5
    ) -> List[Tuple[str, str]]:
        """Extract the latest user-agent interaction pairs from transcript.jsonl."""
        transcript_file = (
            self.gemini_dir
            / "brain"
            / conversation_id
            / ".system_generated"
            / "logs"
            / "transcript.jsonl"
        )
        if not transcript_file.is_file():
            return []

        history: List[Tuple[str, str]] = []
        current_user: Optional[str] = None
        last_agent_resp: str = ""

        try:
            with open(transcript_file, "r", encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                        t = data.get("type")
                        c = data.get("content", "")
                        if t == "USER_INPUT":
                            if current_user is not None and last_agent_resp:
                                history.append((current_user, last_agent_resp))
                            clean = re.sub(r"<USER_REQUEST>|\n?</USER_REQUEST>", "", c).strip()
                            clean = re.sub(r"<ADDITIONAL_METADATA>[\s\S]*?</ADDITIONAL_METADATA>", "", clean).strip()
                            current_user = clean
                            last_agent_resp = ""
                        elif t == "PLANNER_RESPONSE" and c:
                            last_agent_resp = c
                    except json.JSONDecodeError:
                        continue

            if current_user is not None and last_agent_resp:
                history.append((current_user, last_agent_resp))
        except OSError as exc:
            logger.warning(f"Error reading transcript for {conversation_id}: {exc}")
            return []

        return history[-limit:]

    async def get_available_models(self, force_refresh: bool = False) -> List[ModelOption]:
        """Fetch available models in real-time from Antigravity Language Server via GetAvailableModels RPC."""
        now = time.time()
        # Use cache if fresh (< 60s) and not forced
        if not force_refresh and self._cached_models and (now - self._models_cached_at) < 60:
            return self._cached_models

        def _do_rpc() -> Optional[Dict[str, Any]]:
            try:
                ls_addr = self.ensure_connection()
                csrf_token = self._cached_csrf_token
                url = f"http://{ls_addr}/exa.language_server_pb.LanguageServerService/GetAvailableModels"
                headers = {
                    "Content-Type": "application/json",
                    "Connect-Protocol-Version": "1",
                }
                if csrf_token:
                    headers["x-codeium-csrf-token"] = csrf_token

                req = urllib.request.Request(url, data=b"{}", headers=headers, method="POST")
                with urllib.request.urlopen(req, timeout=4) as resp:
                    if resp.status == 200:
                        return json.loads(resp.read().decode("utf-8"))
            except Exception as e:
                logger.debug(f"Failed to fetch live models from Language Server: {e}")
            return None

        loop = asyncio.get_running_loop()
        rpc_result = await loop.run_in_executor(None, _do_rpc)

        if not rpc_result or "response" not in rpc_result:
            # Fallback to current available models if RPC failed
            return AVAILABLE_MODELS

        resp_data = rpc_result.get("response", {})
        models_dict = resp_data.get("models", {})
        agent_sorts = resp_data.get("agentModelSorts", [])

        # 1. Discover recommended order from agentModelSorts
        rec_ids: List[str] = []
        if agent_sorts and "groups" in agent_sorts[0]:
            for g in agent_sorts[0]["groups"]:
                for mid in g.get("modelIds", []):
                    if mid not in rec_ids and mid in models_dict:
                        rec_ids.append(mid)

        # 2. Append any remaining models not in recommended sorts
        for mid in models_dict:
            if mid not in rec_ids and not mid.startswith("tab_") and not mid.startswith("chat_"):
                rec_ids.append(mid)

        if not rec_ids:
            return AVAILABLE_MODELS

        def _get_series_key(m_id: str) -> str:
            if "3.8-flash" in m_id: return "gemini-3.8-flash"
            if "3.7-flash" in m_id: return "gemini-3.7-flash"
            if "3.6-flash" in m_id: return "gemini-3.6-flash"
            if "3.5-flash" in m_id: return "gemini-3.5-flash"
            if "2.5-flash" in m_id: return "gemini-2.5-flash"
            if "pro" in m_id: return "gemini-pro"
            if "sonnet" in m_id: return "claude-sonnet"
            if "opus" in m_id: return "claude-opus"
            if "gpt" in m_id or "120b" in m_id: return "gpt-oss"
            return re.sub(r"-(high|medium|low|thinking|extra-low)$", "", m_id)

        from collections import Counter
        series_keys = [_get_series_key(mid) for mid in rec_ids]
        series_counts = Counter(series_keys)

        series_counter: Dict[str, Tuple[int, int]] = {}
        parsed_models: List[ModelOption] = []
        for mid in rec_ids:
            s_key = _get_series_key(mid)
            if s_key not in series_counter:
                major = len(series_counter) + 1
                sub = 1
            else:
                major, last_sub = series_counter[s_key]
                sub = last_sub + 1
            series_counter[s_key] = (major, sub)

            if series_counts[s_key] == 1:
                code = str(major)
            else:
                code = f"{major}.{sub}"

            info = models_dict.get(mid, {})
            disp = info.get("displayName", mid)
            thinking = info.get("supportsThinking", False)
            quota_info = info.get("quotaInfo", {})
            remaining = quota_info.get("remainingFraction")
            reset_time = quota_info.get("resetTime")

            # Determine badge
            lower_disp = disp.lower()
            if "high" in lower_disp and thinking:
                badge = "High / Thinking"
            elif "high" in lower_disp:
                badge = "High / Fast"
            elif "medium" in lower_disp and thinking:
                badge = "Medium / Thinking"
            elif "medium" in lower_disp:
                badge = "Medium"
            elif "low" in lower_disp and ("pro" in mid or "claude" in mid):
                badge = "Low / Reasoning"
            elif "low" in lower_disp:
                badge = "Low / Fast"
            elif thinking:
                badge = "Thinking"
            else:
                badge = "Standard"

            # Determine tier
            if any(k in mid for k in ("pro", "claude", "gpt")):
                tier = "pro"
            elif "lite" in mid or "3.6" in mid:
                tier = "flash_lite"
            else:
                tier = "flash"

            # Determine friendly description
            if "claude-opus" in mid:
                desc = "Anthropic 顶级超旗舰模型，处理极度复杂的工程与深度推理任务"
            elif "claude-sonnet" in mid:
                desc = "Anthropic 思考增强模型，超强代码生成与复杂算法推理"
            elif "gpt-oss" in mid:
                desc = "开源大参数模型，兼具高容量与中等推理能力"
            elif "3.8" in mid:
                desc = "新一代多模态旗舰 Flash 模型，高智能、极速响应"
            elif "3.7" in mid:
                desc = "经典稳定高效通用模型，综合能力优秀"
            elif "3.6" in mid:
                desc = "极速轻量模型，资源开销低，适合常规问答与代码探索"
            elif "pro" in mid:
                desc = "专业级深层推理模型，适合大型架构与高难度逻辑任务"
            else:
                desc = f"Antigravity Agent 支持的可用模型 ({disp})"

            # Generate smart aliases
            aliases: List[str] = [code, mid]
            # If this is the first (default/highest) option in a major series, also allow typing the major number alone
            if sub == 1:
                aliases.append(str(major))

            # Strip common suffixes/prefixes
            clean_alias = re.sub(r"-(high|medium|low|thinking)", "", mid)
            if clean_alias not in aliases:
                aliases.append(clean_alias)
            # Add short names
            for short in ("sonnet", "opus", "flash", "pro", "120b", "3.8", "3.7", "3.6"):
                if short in mid and short not in aliases:
                    aliases.append(short)

            opt = ModelOption(
                index=major,
                code=code,
                id=mid,
                display_name=disp,
                badge=badge,
                description=desc,
                tier=tier,
                aliases=aliases,
                supports_thinking=thinking,
                quota_remaining=remaining,
                reset_time=reset_time,
                is_recommended=(mid in rec_ids),
            )
            parsed_models.append(opt)

        self._cached_models = parsed_models
        self._models_cached_at = now
        update_available_models(parsed_models)
        logger.info(f"Successfully fetched {len(parsed_models)} live models from Antigravity Language Server")
        return parsed_models
