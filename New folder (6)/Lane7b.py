# python Lane7b.py --sumo dataset1.sumocfg --gui --max-steps 1000 --episodes 1
import os, sys, time, json, pickle,traceback,random, logging, threading,argparse, datetime, warnings
from collections import defaultdict,deque
import numpy as np
import traci
from traci._trafficlight import Logic, Phase
from scheduler import StepScheduler
from config import SUPABASE_URL, PatchedAsyncSupabaseWriter,SUPABASE_KEY, LOG_LEVEL, SUMO_HOME,MAX_PENDING_DB_OPS,LOGIC_MUTATION_COOLDOWN_S,YELLOW_MAX_HOLD_S
from supabase import create_client
from corridor_coordinator import ImprovedCorridorCoordinator
from traffic_light_display import SmartIntersectionTrafficDisplay
logger = logging.getLogger("controller")
logger.setLevel(logging.WARNING)
if not logger.hasHandlers():
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
    logger.addHandler(handler)

# Silence noisy libraries
for noisy in ("httpx", "httpcore", "postgrest", "storage3"):
    logging.getLogger(noisy).setLevel(logging.WARNING)

supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
warnings.filterwarnings('ignore')

def safe_set_logic(tls_id, logic):
    try:
        phases = getattr(logic, "phases", [])
        n = len(phases)
        if n <= 0: return
        cpi = getattr(logic, "currentPhaseIndex", 0)
        safe_cpi = max(0, min(int(cpi), n - 1))
        if safe_cpi != cpi:
            logic = Logic(getattr(logic, "programID", ""), getattr(logic, "type", 0), safe_cpi, phases)
        traci.trafficlight.setCompleteRedYellowGreenDefinition(tls_id, logic)
    except Exception as e:
        logger.info(f"[SAFE LOGIC][ERROR] {tls_id}: {e}")

def safe_set_phase(tls_id, phase_idx, duration=None):
    try:
        logic = traci.trafficlight.getAllProgramLogics(tls_id)[0]
        n_phases = len(getattr(logic, "phases", []))
        if n_phases == 0: return False
        safe_idx = max(0, min(int(phase_idx), n_phases - 1))
        traci.trafficlight.setPhase(tls_id, safe_idx)
        if duration is not None:
            traci.trafficlight.setPhaseDuration(tls_id, float(duration))
        return True
    except Exception as e:
        logger.info(f"[SAFE SET PHASE][ERROR] {tls_id}: {e}")
        return False
class DebugRateLimiter:
    def __init__(self): self._next = {}
    def log(self, key, level, msg, interval_s=1.0):
        now = time.time()
        if now >= self._next.get(key, 0.0):
            logger.log(level, msg)
            self._next[key] = now + interval_s

os.environ.setdefault('SUMO_HOME', r'C:\Program Files (x86)\Eclipse\Sumo')
tools = os.path.join(os.environ['SUMO_HOME'], 'tools')
if tools not in sys.path: sys.path.append(tools)

def verify_supabase_connection():
    try:
        supabase.table("apc_states").select("id").limit(1).execute()
        return True
    except: return False

def fix_phase_states_and_missing_greens(phases, controlled_lanes, min_green=10):
    n = len(controlled_lanes)
    for ph in phases:
        ph.state = ph.state[:n].ljust(n, 'r')
    for i, lane in enumerate(controlled_lanes):
        if not any(ph.state[i].upper() == 'G' for ph in phases):
            phases.append(Phase(min_green, ''.join('G' if j == i else 'r' for j in range(n))))
    return phases

def override_sumo_program_from_supabase(apc, current_phase_idx=None):
    seq = apc.get_full_phase_sequence()
    if not seq: return
    idx = current_phase_idx if current_phase_idx is not None else traci.trafficlight.getPhase(apc.tls_id)
    phases = [Phase(dur, st) for (st, dur) in seq]
    prog = traci.trafficlight.getProgram(apc.tls_id)
    logic = Logic(programID=prog, type=traci.constants.TL_LOGIC_PROGRAM,
                  currentPhaseIndex=idx if 0 <= idx < len(phases) else 0, phases=phases)
    traci.trafficlight.setCompleteRedYellowGreenDefinition(apc.tls_id, logic)
    traci.trafficlight.setProgram(apc.tls_id, logic.programID)

def override_all_tls_with_supabase(controller, current_phase_idx=None):
    for tl_id, apc in getattr(controller, 'adaptive_phase_controllers', {}).items():
        seq = apc.get_full_phase_sequence()
        if not seq: continue
        phases = [Phase(dur, st) for (st, dur) in seq]
        prog = traci.trafficlight.getProgram(tl_id)
        logic = Logic(programID=prog, type=0,
                      currentPhaseIndex=current_phase_idx if current_phase_idx is not None else 0,
                      phases=phases)
        traci.trafficlight.setCompleteRedYellowGreenDefinition(tl_id, logic)
        traci.trafficlight.setProgram(tl_id, logic.programID)




def retry_supabase_operation(operation, max_retries=3):
    for attempt in range(max_retries):
        try: return operation()
        except Exception as e:
            if attempt == max_retries - 1: raise e
            time.sleep(0.5 * (2 ** attempt))

def get_current_logic(tls_id):
    try:
        prog = traci.trafficlight.getProgram(tls_id)
        logics = traci.trafficlight.getAllProgramLogics(tls_id)
        for logic in logics:
            if logic.programID == prog: return logic
        return logics[0] if logics else None
    except Exception as e:
        logger.error(f"[LOGIC] Failed to get current logic for {tls_id}: {e}")
        return None

class AdaptivePhaseController:
    # ========================================
    # 1. INITIALIZATION & SETUP
    # ========================================
    def __init__(self, lane_ids, tls_id, alpha=1.0, min_green=30, max_green=80,
                 r_base=0.5, r_adjust=0.1, severe_congestion_threshold=0.8,
                 large_delta_t=20):
        self.lane_ids = lane_ids
        try:
            for lid in self.lane_ids:
                traci.lane.subscribe(lid, [
                    traci.constants.LAST_STEP_VEHICLE_HALTING_NUMBER,
                    traci.constants.LAST_STEP_MEAN_SPEED,
                    traci.constants.LAST_STEP_VEHICLE_NUMBER,
                    traci.constants.LAST_STEP_VEHICLE_ID_LIST,
                ])
        except Exception:
            pass
        self.tls_id = tls_id
        self.alpha = alpha
        self.min_green = min_green
        self.max_green = max_green
        self.supabase = supabase
        self.traci = traci
        self._sched = StepScheduler()
        self.logger = logger or logging.getLogger(__name__)
        self._db_lock = threading.Lock()
        self.apc_state = {"events": deque(maxlen=5000), "phases": []}
        self._pending_db_ops = []
        self._db_writer = PatchedAsyncSupabaseWriter(self, 
                                             interval=60.0,  # Changed from 1.5 to 10 seconds
                                             max_batch=100)   # Increased batch size
        
        # Add caching for Supabase reads
        self._phase_cache = {}
        self.enable_db_writes = True
        self._phase_cache_ttl = 30.0  # Cache for 30 seconds
        self._phase_cache_time = {}
        self._db_writer.start()
        self.r_base = r_base
        self.r_adjust = r_adjust
        self.severe_congestion_threshold = severe_congestion_threshold
        self.large_delta_t = large_delta_t
        self.phase_repeat_counter = defaultdict(int)
        self.last_served_time = defaultdict(lambda: 0)
        self.severe_congestion_global_cooldown_time = 5
        self._links_map = {lid: traci.lane.getLinks(lid) for lid in lane_ids}
        self._controlled_lanes = traci.trafficlight.getControlledLanes(tls_id)
        self.supabase_available = verify_supabase_connection()
        self._phase_defs = [phase for phase in traci.trafficlight.getCompleteRedYellowGreenDefinition(tls_id)[0].getPhases()]
        self.weights = np.array([0.4, 0.2, 0.2, 0.2])
        self.weight_history = []
        self.metric_history = deque(maxlen=50)
        self.reward_history = deque(maxlen=50)
        self.R_target = r_base
        self.phase_count = 0
        self.rl_agent = None
        self.last_phase_switch_sim_time = 0
        self.pending_request_timestamp = 0
        self.emergency_cooldown = {}
        self.emergency_global_cooldown = 0
        self.last_extended_time = 0
        # In AdaptivePhaseController.__init__ add these queues:
        self._pending_phase_records = []
        self._pending_events = []
        self.protected_left_cooldown = defaultdict(float)
        self.severe_congestion_cooldown = {}
        self.severe_congestion_global_cooldown = 0
        self.last_phase_idx = None
        self.create_yellow_if_missing = True       
        self.last_emergency_event = {}
        self.pending_phase_request = None
        self.pending_extension_request = None
        self.pending_priority_type = None
        self._last_ext_telemetry = -1.0
        self.left_block_steps = defaultdict(int)
        self.left_block_min_steps = 3
        self._logic_cache = None
        self._logic_cache_at = -1.0
        self._dbg = DebugRateLimiter()
        self._logic_cache_ttl = 0.5
        self._last_logic_mutation = -1e9
        self.activation = {
            "phase_idx": None,
            "start_time": 0.0,
            "base_duration": None,
            "desired_total": None
        }
        # --- PATCH: stacked request queue ---
        self.pending_requests = []
        # -------------------------------------
        self.blocked_left_memory = defaultdict(int)  # lane_id -> consecutive detections
        self.blocked_focus_lane = None               # last lane that raised protection
        self.blocked_guard_deadline = 0.0  
        # Add new attributes
        self.cycle_length = 90  # Default cycle length
        self.phase_offset = 0
        self.phase_weights = defaultdict(lambda: 1.0)
        self.phase_duration_multiplier = defaultdict(lambda: 1.0)
        self.flush_mode = False
        self.flush_target_lane = None
        self.serve_empty_greens = False
        self.base_cycle = 90
        
        self.coordinator_phase_mask = None  # optional per-phase mask supplied by corridor coordinator
        self.min_starve_queue = 2          # minimum instantaneous queue to trigger starvation forcing
        self.hysteresis_margin = 0.10      # require +10% score improvement to preempt current phase
        self.low_demand_extend_cap = 4.0   # seconds to keep phase after demand collapses
        self.low_demand_min_halted = 2     # minimum halted vehicles across green to justify further extension
        self.protected_left_min_queue = 5  # raise threshold to avoid over-trigger
        self._load_apc_state_supabase()
        self.preload_phases_from_sumo()
        self._initialize_base_durations()
    def _initialize_base_durations(self):
        logic = self._get_logic()
        for idx, phase in enumerate(logic.getPhases()):
            found = False
            for p in self.apc_state.setdefault("phases", []):
                if p["phase_idx"] == idx:
                    found = True
                    break
            if not found:
                # Cache into apc_state so later lookups see a proper base_duration
                self.apc_state["phases"].append({
                    "phase_idx": idx,
                    "duration": float(phase.duration),
                    "base_duration": float(phase.duration),
                    "state": phase.state,
                    "extended_time": 0.0
                })
                self._save_apc_state_supabase()
                self.save_phase_record_to_supabase(
                    phase_idx=idx,
                    duration=phase.duration,
                    state_str=phase.state,
                    delta_t=0,
                    raw_delta_t=0,
                    penalty=0
                )
    def preload_phases_from_sumo(self):
        for idx, phase in enumerate(self._phase_defs):
            if not any(p['phase_idx'] == idx for p in self.apc_state.get('phases', [])):
                self.save_phase_record_to_supabase(
                    phase_idx=idx,
                    duration=phase.duration,
                    state_str=phase.state,
                    delta_t=0,
                    raw_delta_t=0,
                    penalty=0
                )
    def _decay_blocked_memory(self, step=1):
        self.blocked_left_memory = {k: v-step for k, v in self.blocked_left_memory.items() if v > step}
        if not self.blocked_left_memory:
            self.blocked_focus_lane = None
            self.blocked_guard_deadline = 0.0
    # ========================================
    # 2. DATABASE & STATE PERSISTENCE
    # ========================================
    def _load_apc_state_supabase(self):
        try:
            response = supabase.table("apc_states").select("data, updated_at").eq("tls_id", self.tls_id).eq("state_type", "full").order("updated_at", desc=True).limit(1).execute()
            if response.data and len(response.data) > 0:
                self.apc_state = json.loads(response.data[0]["data"])
                logger.info(f"[Supabase] Loaded state for {self.tls_id} from {response.data[0]['updated_at']}")
            else:
                self.apc_state = {"events": [], "phases": []}
                logger.info(f"[Supabase] No existing state for {self.tls_id}, initializing fresh")
        except Exception as e:
            logger.info(f"[Supabase] Failed to load state for {self.tls_id}: {e}")
            self.apc_state = {"events": [], "phases": []}
    def _save_apc_state_supabase(self):
        if self.supabase_available:
            if len(self._pending_db_ops) >= MAX_PENDING_DB_OPS:
                # Drop oldest in bulk to make room
                drop = max(1, int(0.2 * MAX_PENDING_DB_OPS))
                self._pending_db_ops = self._pending_db_ops[drop:]
            self._pending_db_ops.append(self.apc_state.copy())        
        else:
            logger.info(f"[Supabase] Offline mode - state not saved for {self.tls_id}")
    def flush_pending_supabase_writes(self, max_retries=6, max_batch=1):
        with self._db_lock:
            if not self._pending_db_ops:
                return

            # Only try the latest N states, discard older if needed
            batch = self._pending_db_ops[-max_batch:] if max_batch > 0 else [self._pending_db_ops[-1]]

            for state in batch:
                state_json = json.dumps(state)
                attempt = 0
                delay = 1.0
                while attempt < max_retries:
                    try:
                        response = supabase.table("apc_states").upsert({
                            "tls_id": self.tls_id,
                            "state_type": "full",
                            "data": state_json,
                            "updated_at": datetime.datetime.now().isoformat()
                        }).execute()
                        if response.data:
                            logger.info(f"[Supabase] Successfully synced state for {self.tls_id}")
                            # Remove this state from the queue
                            if state in self._pending_db_ops:
                                self._pending_db_ops.remove(state)
                            break
                    except Exception as e:
                        logger.info(f"[Supabase] Write attempt {attempt+1}/{max_retries} failed: {e}\nData: {state_json[:300]}...")
                        if attempt == max_retries - 1:
                            logger.info(f"[Supabase] All {max_retries} attempts failed, keeping data in queue for {self.tls_id}")
                        else:
                            # Exponential backoff with jitter
                            sleep_time = delay + random.uniform(0, delay * 0.5)
                            time.sleep(sleep_time)
                            delay = min(delay * 2, 30)
                        attempt += 1
    def flush_pending_phase_records(self, max_retries=6, max_batch=200):
        if not getattr(self, "enable_db_writes", False):
            return
        batch = []
        with self._db_lock:
            if not self._pending_phase_records:
                return
            # take the last N (most recent) to reduce stale writes
            take = min(max_batch, len(self._pending_phase_records))
            batch = self._pending_phase_records[-take:]
            self._pending_phase_records = self._pending_phase_records[:-take]
        if not batch:
            return
        # bulk insert with retries
        delay = 1.0
        for attempt in range(max_retries):
            try:
                supabase.table("phase_records").insert(batch).execute()
                return
            except Exception as e:
                logger.info(f"[Supabase] phase_records batch attempt {attempt+1}: {e}")
                if attempt == max_retries - 1:
                    # push back on failure to retry later
                    with self._db_lock:
                        self._pending_phase_records.extend(batch)
                else:
                    time.sleep(delay + random.uniform(0, delay*0.5))
                    delay = min(2*delay, 30.0)
    def flush_pending_events(self, max_retries=6, max_batch=500):
        if not getattr(self, "enable_db_writes", False):
            return
        batch = []
        with self._db_lock:
            if not self._pending_events:
                return
            take = min(max_batch, len(self._pending_events))
            batch = self._pending_events[-take:]
            self._pending_events = self._pending_events[:-take]
        if not batch:
            return
        delay = 1.0
        for attempt in range(max_retries):
            try:
                supabase.table("simulation_events").insert(batch).execute()
                return
            except Exception as e:
                logger.info(f"[Supabase] events batch attempt {attempt+1}: {e}")
                if attempt == max_retries - 1:
                    with self._db_lock:
                        self._pending_events.extend(batch)
                else:
                    time.sleep(delay + random.uniform(0, delay*0.5))
                    delay = min(2*delay, 30.0)
    def save_phase_record_to_supabase(self, phase_idx, duration, state_str, delta_t, raw_delta_t, penalty,
                                    reward=None, bonus=None, weights=None, event_type=None, lanes=None):
        try:
            # honor the toggle; if off, just keep local and return quickly
            if not getattr(self, "enable_db_writes", False):
                return

            rec = self.load_phase_from_supabase(phase_idx)
            base_dur = rec.get("base_duration", self.min_green) if rec else self.min_green
            row = {
                "tls_id": self.tls_id,
                "phase_idx": int(phase_idx),
                "duration": float(duration),
                "base_duration": float(base_dur),
                "state_str": state_str,
                "delta_t": float(delta_t),
                "raw_delta_t": float(raw_delta_t),
                "penalty": float(penalty),
                "reward": reward,
                "bonus": bonus if bonus is not None else 0.0,
                "extended_time": max(0.0, float(duration) - float(base_dur)),
                "event_type": event_type,
                "weights": (weights if weights is not None else self.weights.tolist()),
                "lanes": (lanes if lanes is not None else self.lane_ids[:]),
                "sim_time": float(traci.simulation.getTime()),
                "updated_at": datetime.datetime.now().isoformat()
            }
            # enqueue (drop oldest if queue is too large)
            with self._db_lock:
                cap = max(1000, MAX_PENDING_DB_OPS)
                if len(self._pending_phase_records) >= cap:
                    self._pending_phase_records = self._pending_phase_records[-cap//2:]
                self._pending_phase_records.append(row)
        except Exception as e:
            logger.info(f"[Supabase] queue phase_record failed: {e}")
    def log_event_to_supabase(self, event):
        try:
            if not getattr(self, "enable_db_writes", False):
                return
            row = {
                "tls_id": self.tls_id,
                "event_type": str(event.get("action", "unknown")),
                "event_data": json.dumps(event),
                "sim_time": float(traci.simulation.getTime())
            }
            with self._db_lock:
                cap = max(2000, MAX_PENDING_DB_OPS*2)
                if len(self._pending_events) >= cap:
                    self._pending_events = self._pending_events[-cap//2:]
                self._pending_events.append(row)
        except Exception as e:
            logger.info(f"[Supabase] queue event failed: {e}")
    def _log_apc_event(self, event):
        event["timestamp"] = datetime.datetime.now().isoformat()
        event["sim_time"] = traci.simulation.getTime()
        event["tls_id"] = self.tls_id
        event["weights"] = self.weights.tolist()
        event["bonus"] = getattr(self, "last_bonus", 0)
        event["penalty"] = getattr(self, "last_penalty", 0)
        
        # Add to in-memory state
        self.apc_state["events"].append(event)
        self._save_apc_state_supabase()
        
        # ALSO queue for database writing
        self.log_event_to_supabase(event)  # <-- Add this line
    def log_phase_to_event_log(self, phase_idx, new_duration):
        # Find phase info in self.apc_state["phases"]
        phase = next((p for p in self.apc_state["phases"] if p["phase_idx"] == phase_idx), None)
        if not phase:
            base_duration = new_duration
            extended_time = 0
        else:
            base_duration = phase.get("base_duration", phase.get("duration", new_duration))
            extended_time = new_duration - base_duration
        # Now append to the event log (controller.phase_events)
        if hasattr(self, "controller") and hasattr(self.controller, "phase_events"):
            self.controller.phase_events.append({
                "tls_id": self.tls_id,
                "phase_idx": phase_idx,
                "base_duration": base_duration,
                "duration": new_duration,
                "extended_time": extended_time,
                "timestamp": datetime.datetime.now().isoformat()
            })
    def update_phase_duration_record(self, phase_idx, new_duration, extended_time=0):
        updated = False
        for p in self.apc_state.get("phases", []):
            if p["phase_idx"] == phase_idx:
                # Preserve original base_duration once set; do not shrink it later
                if "base_duration" not in p:
                    p["base_duration"] = float(new_duration - extended_time)
                p["duration"] = new_duration
                p["extended_time"] = extended_time
                updated = True
        if updated:
            self._save_apc_state_supabase()
        self._log_apc_event({
            "action": "phase_duration_update",
            "phase_idx": phase_idx,
            "duration": new_duration,
            "extended_time": extended_time,
            "tls_id": self.tls_id
        })
    def load_phase_from_supabase(self, phase_idx=None):
        # 1) Try cached state first
        for p in self.apc_state.get("phases", []):
            if p.get("phase_idx") == phase_idx:
                return p
        # 2) Fallback to SUMO logic if not cached; also cache it into apc_state
        try:
            logic = self._get_logic()
            phases = logic.getPhases()
            if phase_idx is not None and 0 <= phase_idx < len(phases):
                ph = phases[phase_idx]
                record = {
                    "phase_idx": phase_idx,
                    "duration": float(getattr(ph, "duration", self.min_green)),
                    "base_duration": float(getattr(ph, "duration", self.min_green)),
                    "state": ph.state,
                    "extended_time": 0.0,
                }
                self.apc_state.setdefault("phases", []).append(record.copy())
                self._save_apc_state_supabase()
                return record
        except Exception as e:
            logger.info(f"[WARN] load_phase_from_supabase fallback failed for phase {phase_idx}: {e}")
        return None
    # ========================================
    # 3. CORE PHASE LOGIC & MANAGEMENT
    # ========================================    
    def _get_logic(self):
        now = traci.simulation.getTime()
        # If controller has a shared cache, prefer it
        try:
            controller = getattr(self, "controller", None)
            if controller is not None and isinstance(getattr(controller, "tl_logic_cache", None), dict):
                entry = controller.tl_logic_cache.get(self.tls_id)
                if entry and (now - entry.get("at", -1)) <= getattr(self, "_logic_cache_ttl", 0.5):
                    return entry.get("logic")
                # fetch fresh
                logic = get_current_logic(self.tls_id)
                controller.tl_logic_cache[self.tls_id] = {"logic": logic, "at": now}
                return logic
        except Exception:
            # fallthrough to APC-local cache if something goes wrong
            pass

        # APC-local cache fallback (existing behaviour)
        if self._logic_cache is None or now - self._logic_cache_at > self._logic_cache_ttl:
            try:
                self._logic_cache = get_current_logic(self.tls_id)
                self._logic_cache_at = now
            except Exception:
                self._logic_cache = None
        return self._logic_cache
    def _invalidate_logic_cache(self, tl_id=None):
        # invalidate APC-local cache
        self._logic_cache = None
        self._logic_cache_at = -1.0
        # also invalidate controller-level cache if present
        try:
            controller = getattr(self, "controller", None)
            if controller is not None and hasattr(controller, "_invalidate_logic_cache"):
                # pass self.tls_id so controller clears that entry only
                controller._invalidate_logic_cache(self.tls_id)
        except Exception:
            pass
    def _safe_phase_index(self, idx, force_reload=False):
        try:
            if force_reload:
                self._invalidate_logic_cache()
            logic = self._get_logic()
            if not logic or len(logic.getPhases()) <= 0:
                return None
            n = len(logic.getPhases())
            return max(0, min(idx, n - 1))
        except Exception:
            return None
    def _apply_phase(self, phase_idx, duration):
        try:
            # Always clamp against fresh logic
            safe_idx = self._safe_phase_index(phase_idx, force_reload=True)
            if safe_idx is None:
                #logger.info(f"[APPLY_PHASE] {self.tls_id}: No valid phases to switch to.")
                return False

            controller = getattr(self, "controller", None)
            safe_set_func = getattr(controller, "_safe_set_phase", None)

            # First try: controller-level setter if available (returns bool)
            if safe_set_func:
                ok = bool(safe_set_func(
                    self.tls_id,
                    int(safe_idx),
                    duration=float(duration) if duration is not None else None
                ))
                if ok:
                    return True
                logger.info(f"[APPLY_PHASE][WARN] Controller setter returned False for {self.tls_id}. Falling back.")

            # Second try: direct safe_set_phase (returns bool)
            # Re-clamp once more defensively
            safe_idx = self._safe_phase_index(phase_idx, force_reload=True)
            if safe_idx is None:
                return False

            ok2 = safe_set_phase(
                self.tls_id,
                int(safe_idx),
                duration=float(duration) if duration is not None else None
            )
            if ok2:
                return True

            logger.info(f"[APPLY_PHASE][ERROR] Both controller and fallback setters failed for {self.tls_id}.")
            return False

        except Exception as e:
            logger.info(f"[APPLY_PHASE][ERROR] Unexpected failure: {e}")
            return False  
    def _can_mutate_logic(self):
        now = traci.simulation.getTime()
        if now - getattr(self, "_last_logic_mutation", -1e9) < LOGIC_MUTATION_COOLDOWN_S:
            logger.info(f"[RATE-LIMIT] Skipping logic mutation; cooldown {LOGIC_MUTATION_COOLDOWN_S}s")
            return False
        self._last_logic_mutation = now
        return True    
    def set_phase_from_API(self, phase_idx, requested_duration=None):
        logger.info(f"[FIXED] set_phase_from_API({phase_idx}, requested_duration={requested_duration})")

        # Always refresh logic before using phase indices (prevents stale-size issues)
        self._invalidate_logic_cache()
        logic = self._get_logic()
        n_phases = len(logic.getPhases()) if logic else 0
        if n_phases == 0:
            logger.info(f"[FIXED] {self.tls_id}: no phases available; ignoring set_phase_from_API")
            return False

        # Clamp the target phase against the (fresh) logic
        phase_idx = self._safe_phase_index(int(phase_idx), force_reload=True)
        if phase_idx is None:
            logger.info(f"[FIXED] {self.tls_id}: invalid phase index; ignoring set_phase_from_API")
            return False

        # Load phase metadata and derive base duration
        phase_record = self.load_phase_from_supabase(phase_idx)
        if phase_record:
            base_duration = phase_record.get("base_duration", phase_record.get("duration", self.min_green))
        else:
            try:
                phs = logic.getPhases()
                base_duration = float(phs[phase_idx].duration) if 0 <= phase_idx < len(phs) else self.min_green
            except Exception:
                base_duration = self.min_green

        desired_total = requested_duration if requested_duration is not None else base_duration
        desired_total = float(np.clip(desired_total, self.min_green, self.max_green))

        # Determine current phase and clamp it, too
        try:
            current_phase = traci.trafficlight.getPhase(self.tls_id)
        except Exception:
            current_phase = phase_idx
        current_phase = self._safe_phase_index(current_phase) or phase_idx

        # SAFETY: avoid activating a phase that has no green heads at all
        try:
            target_state = logic.getPhases()[phase_idx].state
            if 'G' not in target_state.upper():
                # Find a better fallback that has greens (prefer the most greens)
                best_idx, best_g = None, -1
                for i, ph in enumerate(logic.getPhases()):
                    gcount = ph.state.upper().count('G')
                    if gcount > best_g:
                        best_idx, best_g = i, gcount
                if best_idx is not None and best_g > 0:
                    logger.info(f"[SAFE] {self.tls_id}: target phase {phase_idx} has no greens; using {best_idx} instead")
                    phase_idx = best_idx
                else:
                    logger.info(f"[SAFE] {self.tls_id}: no green-bearing phase available; aborting switch")
                    return False
        except Exception as e:
            logger.info(f"[SAFE] Could not validate green heads on phase {phase_idx}: {e}")

        # Perform switch using hardened helper
        if current_phase != phase_idx:
            used_yellow = self.insert_yellow_phase_if_needed(current_phase, phase_idx)
            if used_yellow:
                # Yellow insertion can mutate logic; refresh and re-clamp once more defensively
                self._invalidate_logic_cache()
                logic = self._get_logic()
                phase_idx = self._safe_phase_index(phase_idx)

            ok = self._apply_phase(phase_idx, duration=desired_total)
            if not ok:
                logger.info(f"[FIXED] {self.tls_id}: phase apply failed")
                return False
            self._reset_activation(phase_idx, base_duration, desired_total)
        else:
            # Same phase: ensure activation is aligned and optionally update remaining
            if self.activation["phase_idx"] != phase_idx or self.activation["start_time"] == 0.0:
                self._reset_activation(phase_idx, base_duration, desired_total)
            if requested_duration is not None:
                self._maybe_update_phase_remaining(desired_total)

        # Book-keeping and UI/event telemetry
        elapsed = self._get_phase_elapsed()
        remaining = self._get_phase_remaining()
        total_now = max(desired_total, elapsed + remaining)
        extended_time = max(0.0, total_now - base_duration)
        self.update_phase_duration_record(phase_idx, total_now, extended_time)
        if hasattr(self, "log_phase_to_event_log"):
            self.log_phase_to_event_log(phase_idx, total_now)

        logger.info(f"[FIXED/PATCH] {self.tls_id} Phase {current_phase} → {phase_idx}, desired_total={desired_total:.1f}s, now_total≈{total_now:.1f}s, extended≈{extended_time:.1f}s")
        return True
    def insert_yellow_phase_if_needed(self, from_phase, to_phase):
        if from_phase == to_phase:
            return False
        try:
            logic = self._get_logic()
            if not logic:
                return False

            # Validate indices against current logic
            n = len(logic.phases)
            if n == 0:
                return False
            if not (0 <= from_phase < n and 0 <= to_phase < n):
                self.logger.info(f"[YELLOW] {self.tls_id}: invalid indices from={from_phase}, to={to_phase}, n={n}")
                return False

            from_state = logic.phases[from_phase].state
            to_state = logic.phases[to_phase].state
            nmin = min(len(from_state), len(to_state))

            yellow_needed = False
            yellow = list(from_state)
            for i in range(nmin):
                if from_state[i].upper() == 'G' and to_state[i].upper() == 'R':
                    yellow[i] = 'y'
                    yellow_needed = True

            if not yellow_needed:
                return False

            yellow_state_str = ''.join(yellow)

            # Try existing yellow
            yellow_idx = None
            for idx, ph in enumerate(logic.phases):
                if ph.state == yellow_state_str:
                    yellow_idx = idx
                    break

            if yellow_idx is not None:
                safe_idx = self._safe_phase_index(yellow_idx, force_reload=True)
                if safe_idx is None:
                    return False
                if self._apply_phase(safe_idx, duration=3.0):
                    self._log_apc_event({
                        "action": "yellow_transition",
                        "from_phase": from_phase,
                        "to_phase": to_phase,
                        "yellow_phase": safe_idx,
                        "yellow_state": yellow_state_str
                    })
                    logger.info(f"[YELLOW] Transition via existing phase {safe_idx}")
                    return True
                return False

            if not self.create_yellow_if_missing or not self._can_mutate_logic():
                logger.info("[YELLOW] No matching yellow phase found; skipping (auto-create disabled)")
                return False

            phases = list(logic.phases)
            max_phases = 12
            yellow_phase = traci.trafficlight.Phase(3.0, yellow_state_str)

            if len(phases) < max_phases:
                new_idx = len(phases)
                phases.append(yellow_phase)
            else:
                exclude = [from_phase, to_phase]
                idx_to_overwrite = self.find_phase_to_overwrite(yellow_state_str, exclude_indices=exclude)
                if idx_to_overwrite is None:
                    logger.info("[YELLOW] Phase limit reached and no overwrite candidate; skipping yellow creation")
                    return False
                new_idx = idx_to_overwrite
                phases[new_idx] = yellow_phase

            new_logic = traci.trafficlight.Logic(
                logic.programID, logic.type, min(logic.currentPhaseIndex, len(phases)-1), phases
            )
            traci.trafficlight.setCompleteRedYellowGreenDefinition(self.tls_id, new_logic)
            self._invalidate_logic_cache()
            if hasattr(self, "controller") and hasattr(self.controller, "_invalidate_logic_cache"):
                self.controller._invalidate_logic_cache(self.tls_id)

            safe_idx = self._safe_phase_index(new_idx, force_reload=True)
            if safe_idx is None:
                return False
            if self._apply_phase(safe_idx, duration=3.0):
                self._log_apc_event({
                    "action": "yellow_transition_created",
                    "from_phase": from_phase,
                    "to_phase": to_phase,
                    "yellow_phase": safe_idx,
                    "yellow_state": yellow_state_str
                })
                logger.info(f"[YELLOW] Created and used new yellow phase {safe_idx}")
                return True
            return False

        except Exception as e:
            logger.info(f"[ERROR] Yellow phase insertion failed: {e}")
            return False    # --- PATCH 4: AdaptivePhaseController.create_or_extend_phase (invalidate controller cache after mutation) ---
    def log_phase_switch(self, new_phase_idx):
        current_time = traci.simulation.getTime()
        elapsed = current_time - self.last_phase_switch_sim_time

        if elapsed < self.min_green and not self.check_priority_conditions():
            logger.info(f"[MIN_GREEN BLOCK] Phase switch blocked (elapsed: {elapsed:.1f}s < {self.min_green}s)")
            return False

        if self.last_phase_idx == new_phase_idx:
            logger.info(f"[PHASE SWITCH BLOCKED] Flicker prevention triggered for {self.tls_id}")
            return False

        self._invalidate_logic_cache()
        logic = self._get_logic()
        n_phases = len(logic.getPhases()) if logic else 0
        if n_phases == 0:
            logger.info(f"[WARN] No phases available at {self.tls_id}")
            return False
        new_phase_idx = self._safe_phase_index(new_phase_idx, force_reload=False)
        if new_phase_idx is None:
            return False

        try:
            current_phase = traci.trafficlight.getPhase(self.tls_id)
        except Exception:
            current_phase = new_phase_idx
        self.insert_yellow_phase_if_needed(current_phase, new_phase_idx)

        try:
            controller = getattr(self, "controller", None)
            safe_set_func = getattr(controller, "_safe_set_phase", None)
            if safe_set_func:
                self._apply_phase(new_phase_idx, duration=max(self.min_green, self.max_green))
            else:
                self._apply_phase(new_phase_idx, duration=max(self.min_green, self.max_green))

            # Refresh current phase after change
            new_phase = traci.trafficlight.getPhase(self.tls_id)
            new_state = traci.trafficlight.getRedYellowGreenState(self.tls_id)

            self.last_phase_idx = new_phase_idx
            self.last_phase_switch_sim_time = current_time

            phase_was_rl_created = False
            phase_pkl = self.load_phase_from_supabase(new_phase_idx)
            if phase_pkl and phase_pkl.get("rl_created"):
                phase_was_rl_created = True

            event = {
                "action": "phase_switch",
                "old_phase": current_phase,
                "new_phase": new_phase,
                "old_state": "",
                "new_state": new_state,
                "reward": getattr(self, "last_R", None),
                "weights": self.weights.tolist(),
                "bonus": getattr(self, "last_bonus", 0),
                "penalty": getattr(self, "last_penalty", 0),
                "rl_created": phase_was_rl_created,
                "phase_idx": new_phase_idx
            }
            self._log_apc_event(event)
            logger.info(f"\n[PHASE SWITCH] {self.tls_id}: {current_phase}→{new_phase}")
            logger.info(f"  New state: {new_state}")
            logger.info(f"  Weights: {self.weights}, Bonus: {getattr(self, 'last_bonus', 0)}, Penalty: {getattr(self, 'last_penalty', 0)}")
            if phase_was_rl_created:
                logger.info(f"  [INFO] RL agent's phase is now in use (phase {new_phase_idx})")
            return True
        except Exception as e:
            logger.info(f"[ERROR] Phase switch failed: {e}")
            return False 
    def _delayed_phase_switch(self, phase_idx, requested_duration):
        try:
            safe_idx = self._safe_phase_index(phase_idx, force_reload=True)
            if safe_idx is None:
                logger.info(f"[ERROR] No valid phases to switch to for {self.tls_id}")
                return
            self._apply_phase(safe_idx, duration=requested_duration)
            logger.info(f"[DELAYED SWITCH] Completed transition to phase {safe_idx}")
        except Exception as e:
            logger.info(f"[ERROR] Delayed phase switch failed: {e}")    
    def is_phase_ending(self, min_left=2.0, frac=0.1):
        """Phase considered ending if <min_left seconds remain or <frac of total duration remains."""
        try:
            now = traci.simulation.getTime()
            remaining = traci.trafficlight.getNextSwitch(self.tls_id) - now
            total = traci.trafficlight.getPhaseDuration(self.tls_id)
            return remaining <= float(min_left) or remaining <= float(total) * float(frac)
        except Exception:
            return False
    def _reset_activation(self, phase_idx, base_duration, desired_total):
        now = traci.simulation.getTime()
        self.activation["phase_idx"] = phase_idx
        self.activation["start_time"] = now
        self.activation["base_duration"] = float(base_duration)
        self.activation["desired_total"] = float(desired_total)
        self.last_phase_switch_sim_time = now  # keep existing behavior
    # ========================================
    # 4. PHASE CREATION & MODIFICATION
    # ========================================    
    def add_new_phase(self, green_lanes, green_duration=None, yellow_duration=3):
        logic = self._get_logic()
        controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
        phase_state = ['r'] * len(controlled_lanes)
        for lane in green_lanes:
            if lane in controlled_lanes:
                idx = controlled_lanes.index(lane)
                phase_state[idx] = 'G'
        green_state_str = "".join(phase_state)
        # Green phase
        new_green_phase = traci.trafficlight.Phase(green_duration or self.max_green, green_state_str)
        # Yellow phase (for these lanes only)
        yellow_state = ['r'] * len(controlled_lanes)
        for lane in green_lanes:
            if lane in controlled_lanes:
                idx = controlled_lanes.index(lane)
                yellow_state[idx] = 'y'
        yellow_state_str = "".join(yellow_state)
        new_yellow_phase = traci.trafficlight.Phase(yellow_duration, yellow_state_str)
        # Append both phases
        phases = list(logic.getPhases()) + [new_green_phase, new_yellow_phase]
        new_logic = traci.trafficlight.Logic(
            logic.programID, logic.type, len(phases) - 2, phases
        )
        traci.trafficlight.setCompleteRedYellowGreenDefinition(self.tls_id, new_logic)
        self._invalidate_logic_cache()
        return len(phases) - 2  # index of new green phase
    def create_or_extend_phase(self, green_lanes, delta_t):
        logic = self._get_logic()
        controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
        valid_green_lanes = [lane for lane in green_lanes if lane in controlled_lanes]
        
        if not valid_green_lanes:
            logger.info(f"[WARNING] No valid green lanes provided for phase creation")
            return None

        new_state = self.create_phase_state(green_lanes=valid_green_lanes)
        phase_idx = None
        base_duration = self.min_green

        for idx, phase in enumerate(logic.getPhases()):
            if phase.state == new_state:
                phase_record = self.load_phase_from_supabase(idx)
                if phase_record and "duration" in phase_record:
                    base_duration = phase_record["duration"]
                else:
                    base_duration = phase.duration
                phase_idx = idx
                break

        duration = float(np.clip(base_duration + delta_t, self.min_green, self.max_green))
        
        if phase_idx is not None:
            logger.info(f"[PHASE EXTEND] Extending phase {phase_idx} from {base_duration}s to {duration}s (delta_t={delta_t}s)")
            self.save_phase_record_to_supabase(phase_idx, duration, new_state, delta_t, delta_t, penalty=0)
            self.set_phase_from_API(phase_idx, requested_duration=duration)
            if hasattr(self, "update_display"):
                self.update_display(phase_idx, duration)
            return phase_idx
        else:
            if not self._can_mutate_logic():
                return None
            logger.info(f"[PHASE CREATE] Creating new phase with state: {new_state}, duration: {duration}s")
            new_phase = traci.trafficlight.Phase(duration, new_state)
            phases = list(logic.getPhases())
            new_phase_idx = len(phases)
            phases.append(new_phase)

            new_logic = traci.trafficlight.Logic(
                logic.programID, 
                logic.type, 
                min(logic.currentPhaseIndex, len(phases)-1),
                [traci.trafficlight.Phase(duration=ph.duration, state=ph.state) for ph in phases]
            )
            traci.trafficlight.setCompleteRedYellowGreenDefinition(self.tls_id, new_logic)
            self._invalidate_logic_cache()
            # Invalidate controller cache too
            if hasattr(self, "controller") and hasattr(self.controller, "_invalidate_logic_cache"):
                self.controller._invalidate_logic_cache(self.tls_id)

            self.save_phase_record_to_supabase(new_phase_idx, duration, new_state, delta_t, delta_t, penalty=0)
            self.set_phase_from_API(new_phase_idx, requested_duration=duration)
            if hasattr(self, "update_display"):
                self.update_display(new_phase_idx, duration)
            logger.info(f"[PHASE CREATE] New phase created at index {new_phase_idx}")
            return new_phase_idx
    def overwrite_phase(self, phase_idx, new_state, new_duration):
        if not self._can_mutate_logic():
            return False
        try:
            logic = self._get_logic()
            phases = list(logic.phases)
            if phase_idx >= len(phases):
                logger.info(f"[ERROR] Cannot overwrite phase {phase_idx}: index out of range")
                return False

            new_phase = traci.trafficlight.Phase(new_duration, new_state)
            phases[phase_idx] = new_phase
            new_logic = traci.trafficlight.Logic(
                logic.programID, logic.type, logic.currentPhaseIndex, phases
            )
            traci.trafficlight.setCompleteRedYellowGreenDefinition(self.tls_id, new_logic)
            self._invalidate_logic_cache()
            # Invalidate controller cache too
            if hasattr(self, "controller") and hasattr(self.controller, "_invalidate_logic_cache"):
                self.controller._invalidate_logic_cache(self.tls_id)

            self.phase_usage_count[phase_idx] = 0
            self.phase_last_used[phase_idx] = traci.simulation.getTime()
            self.save_phase_record_to_supabase(
                phase_idx=phase_idx,
                duration=new_duration,
                state_str=new_state,
                delta_t=0,
                raw_delta_t=0,
                penalty=0,
                event_type="phase_overwrite"
            )
            self._log_apc_event({
                "action": "phase_overwrite",
                "phase_idx": phase_idx,
                "old_state": logic.phases[phase_idx].state if phase_idx < len(logic.phases) else "unknown",
                "new_state": new_state,
                "new_duration": new_duration,
                "sim_time": traci.simulation.getTime()
            })
            logger.info(f"[PHASE OVERWRITE] Successfully overwrote phase {phase_idx} with new state: {new_state}")
            return True

        except Exception as e:
            logger.info(f"[ERROR] Phase overwrite failed: {e}")
            import traceback
            traceback.logger.info_exc()
            return False
    def find_phase_to_overwrite(self, new_state, exclude_indices=None):
        logic = self._get_logic()
        phases = logic.phases
        
        if exclude_indices is None:
            exclude_indices = []
        
        # Don't overwrite the current phase
        current_phase = traci.trafficlight.getPhase(self.tls_id)
        exclude_indices.append(current_phase)
        
        # Calculate phase usage statistics if we haven't already
        if not hasattr(self, "phase_usage_count"):
            self.phase_usage_count = defaultdict(int)
            self.phase_last_used = defaultdict(lambda: 0)
        
        # Score each phase based on multiple factors
        phase_scores = {}
        current_time = traci.simulation.getTime()
        
        for idx, phase in enumerate(phases):
            # Skip excluded phases
            if idx in exclude_indices:
                continue
                
            # Skip yellow phases
            if 'y' in phase.state:
                continue
                
            # Calculate similarity score (how similar is this phase to the new one?)
            similarity = sum(1 for a, b in zip(phase.state, new_state) if a == b) / len(new_state)
            
            # Calculate usage score (less used phases get higher scores)
            usage_score = 1.0 / (self.phase_usage_count.get(idx, 1) + 1)
            
            # Calculate recency score (older phases get higher scores)
            time_since_used = current_time - self.phase_last_used.get(idx, 0)
            recency_score = min(1.0, time_since_used / 1000)  # Normalize to [0,1]
            
            # Combined score (higher = better to overwrite)
            # We prefer to overwrite phases that are:
            # 1. Similar to the new phase (easier transition)
            # 2. Used infrequently
            # 3. Haven't been used recently
            score = (
                0.4 * similarity +   # Weight for phase similarity
                0.4 * usage_score +  # Weight for infrequent usage 
                0.2 * recency_score  # Weight for recent usage
            )
            
            phase_scores[idx] = score
            
        # If we have no valid phases to overwrite, return None
        if not phase_scores:
            return None
            
        # Find the phase with the highest score
        best_phase_idx = max(phase_scores, key=phase_scores.get)
        
        logger.info(f"[PHASE OVERWRITE] Selecting phase {best_phase_idx} to overwrite (score: {phase_scores[best_phase_idx]:.2f})")
        return best_phase_idx
    def create_phase_state(self, green_lanes=None, yellow_lanes=None, red_lanes=None):
        controlled_links = traci.trafficlight.getControlledLinks(self.tls_id)
        n = len(controlled_links)
        state = ['r'] * n  # default all red

        def set_lane_color(lane, ch):
            if not lane:
                return
            for i, link in enumerate(controlled_links):
                try:
                    from_lane = link[0][0]
                except Exception:
                    continue
                if from_lane == lane:
                    state[i] = ch

        # Apply in strict priority: red, yellow, green
        if red_lanes:
            for ln in red_lanes:
                set_lane_color(ln, 'r')
        if yellow_lanes:
            for ln in yellow_lanes:
                set_lane_color(ln, 'y')
        if green_lanes:
            for ln in green_lanes:
                set_lane_color(ln, 'G')

        return "".join(state)
    def generate_optimal_phase_set(self, controlled_lanes):
        phases = []
        phase_state_set = set()

        logger.info(f"[PHASE GENERATION] Creating optimal phase set for {len(controlled_lanes)} lanes")

        # 1. Create green phases for each lane (ensures every lane gets served)
        for lane in controlled_lanes:
            green_state = self.create_phase_state(green_lanes=[lane])
            if green_state not in phase_state_set:
                phases.append(traci.trafficlight.Phase(self.min_green, green_state))
                phase_state_set.add(green_state)

        # 2. Create combination phases (optional: here, only pairs on different approaches)
        for i, lane1 in enumerate(controlled_lanes):
            for j, lane2 in enumerate(controlled_lanes[i+1:], i+1):
                try:
                    edge1 = traci.lane.getEdgeID(lane1)
                    edge2 = traci.lane.getEdgeID(lane2)
                    if edge1 != edge2:
                        combo_state = self.create_phase_state(green_lanes=[lane1, lane2])
                        if combo_state not in phase_state_set:
                            phases.append(traci.trafficlight.Phase(self.min_green, combo_state))
                            phase_state_set.add(combo_state)
                except Exception:
                    continue

        # 3. Generate ALL required yellow transitions for every phase-to-phase switch
        yellow_phases = []
        yellow_state_set = set()
        yellow_duration = 3
        n_phases = len(phases)
        for i, phase_from in enumerate(phases):
            for j, phase_to in enumerate(phases):
                if i == j:
                    continue
                from_state = phase_from.state
                to_state = phase_to.state
                yellow_needed = False
                yellow_state = []
                for k in range(min(len(from_state), len(to_state))):
                    if from_state[k].upper() == 'G' and to_state[k].upper() == 'R':
                        yellow_state.append('y')
                        yellow_needed = True
                    else:
                        if from_state[k].upper() == 'G' and to_state[k].upper() == 'R':
                            yellow_state.append('y')
                            yellow_needed = True
                        else:
                            yellow_state.append(from_state[k])
                yellow_state_str = ''.join(yellow_state)
                # Only add if needed, not duplicate, and not already a green phase
                if yellow_needed and yellow_state_str not in phase_state_set and yellow_state_str not in yellow_state_set:
                    yellow_phases.append(traci.trafficlight.Phase(yellow_duration, yellow_state_str))
                    yellow_state_set.add(yellow_state_str)

        # 4. Add all yellow phases to main phase list
        phases.extend(yellow_phases)
        phase_state_set.update(yellow_state_set)

        # 5. Verify every lane has at least one green phase
        served = [False] * len(controlled_lanes)
        for phase in phases:
            for idx, ch in enumerate(phase.state):
                if ch.upper() == 'G':
                    served[idx] = True
        for idx, was_served in enumerate(served):
            if not was_served:
                state = ''.join(['G' if i == idx else 'r' for i in range(len(controlled_lanes))])
                if state not in phase_state_set:
                    phases.append(traci.trafficlight.Phase(self.min_green, state))
                    phase_state_set.add(state)

        logger.info(f"[PHASE GENERATION] Final phase set: {len(phases)} phases ({len(yellow_phases)} yellow transitions)")

        # 6. Log all phases for debugging
        for i, phase in enumerate(phases):
            phase_type = "YELLOW" if 'y' in phase.state else "GREEN"
            green_lanes = [controlled_lanes[j] for j in range(min(len(phase.state), len(controlled_lanes)))
                           if phase.state[j].upper() == 'G']
            logger.info(f"  Phase {i}: {phase.state} ({phase_type}, duration={phase.duration}s) - Serves: {green_lanes}")

        return phases
    def ensure_phases_have_green(self):

        logic = self._get_logic()
        changed = False
        for idx, phase in enumerate(logic.getPhases()):
            if 'G' not in phase.state:
                # Find the first red (or any) and turn it green
                state_list = list(phase.state)
                for i, ch in enumerate(state_list):
                    if ch == 'r':
                        state_list[i] = 'G'
                        break
                else:
                    # If no red, just set the first position to green as a fallback
                    state_list[0] = 'G'
                new_state = ''.join(state_list)
                logger.info(f"[PATCH] Phase {idx} had no green, fixing: {phase.state} → {new_state}")
                # Overwrite the phase with corrected state
                self.overwrite_phase(idx, new_state, phase.duration)
                changed = True
        if changed:
            logger.info("[PATCH] All phases now have at least one green light.")
    def add_new_phase_for_lane(self, lane_id, green_duration=None, yellow_duration=3):
        idx = self.add_new_phase(
            green_lanes=[lane_id],
            green_duration=green_duration or self.max_green,
            yellow_duration=yellow_duration
        )
        # PATCH: Log explicit RL phase creation for audit trail
        self._log_apc_event({
            "action": "add_new_phase_for_lane",
            "lane_id": lane_id,
            "phase_idx": idx,
            "rl_created": True,
        })
        return idx
    def find_or_create_phase_for_lane(self, lane_id):
         logic = self._get_logic()
         controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
         
         if lane_id not in controlled_lanes:
             logger.info(f"[DEBUG] find_or_create_phase_for_lane: {lane_id} not in controlled_lanes")
             return None
             
         # Look for existing phase
         lane_idx = controlled_lanes.index(lane_id)
         for idx, phase in enumerate(logic.getPhases()):
             if lane_idx < len(phase.state) and phase.state[lane_idx] in 'Gg':
                 logger.info(f"[DEBUG] Existing phase {idx} serves lane {lane_id}")
                 return idx
         
         # Create new phase if needed
         logger.info(f"[DEBUG] No phase found for {lane_id}, creating new phase")
         return self.add_new_phase_for_lane(lane_id)
    def find_phase_for_lane(self, lane_id):
        try:
            logic = self._get_logic()
            controlled_links = traci.trafficlight.getControlledLinks(self.tls_id)
            # collect this lane's link indices
            idxs = [i for i, lk in enumerate(controlled_links) if lk[0][0] == lane_id]
            if not idxs:
                return None
            for idx, phase in enumerate(logic.getPhases()):
                st = phase.state
                if any((i < len(st) and st[i].upper() == 'G') for i in idxs):
                    return idx
            return None
        except Exception as e:
            logger.info(f"[ERROR] find_phase_for_lane failed for {lane_id}: {e}")
            return None
    def reorganize_or_create_phase(self, lane_id, event_type):
        try:
            is_left_turn = any(link[6] == 'l' for link in traci.lane.getLinks(lane_id))
            now = traci.simulation.getTime()
            if is_left_turn and now - self.protected_left_cooldown[lane_id] < 60:
                return False
                
            target_phase = self.find_or_create_phase_for_lane(lane_id)
            if target_phase is not None:
                # PATCH: Use pending request instead of immediate switch
                priority = 'emergency' if event_type == 'emergency_vehicle' else 'severe_congestion'
                success = self.request_phase_change(target_phase, priority_type=priority)
                
                if success:
                    self._log_apc_event({
                        "action": "reorganize_phase_requested",
                        "lane_id": lane_id,
                        "event_type": event_type,
                        "requested_phase": target_phase,
                        "priority_type": priority
                    })
                    if is_left_turn:
                        self.protected_left_cooldown[lane_id] = now
                return success
            return False
        except Exception as e:
            logger.info(f"[ERROR] Phase reorganization failed: {e}")
            return False
    def check_phase_limit(self):
        logic = self._get_logic()
        num_phases = len(logic.phases)
        max_phases = 12  # SUMO hard limit
        
        if num_phases >= max_phases:
            logger.info(f"[WARNING] Traffic light {self.tls_id} at maximum phase limit ({max_phases})")
            return True
        return False
    # ========================================
    # 5. PHASE TIMING & DURATION CONTROL
    # ========================================    
    def enforce_min_green(self):
        current_sim_time = traci.simulation.getTime()
        elapsed = current_sim_time - self.last_phase_switch_sim_time
        if elapsed < self.min_green:
            logger.info(f"[MIN_GREEN ENFORCED] {self.tls_id}: Only {elapsed:.2f}s since last switch, min_green={self.min_green}s")
            return False
        return True
    def adjust_phase_duration(self, delta_t):
        try:
            # Enforce minimum green time
            if not self.enforce_min_green() and not self.check_priority_conditions():
                logger.info("[ADJUST BLOCKED] Min green active or priority conditions met.")
                return traci.trafficlight.getPhaseDuration(self.tls_id)

            current_phase = traci.trafficlight.getPhase(self.tls_id)
            # Initialize activation if missing
            if self.activation["phase_idx"] != current_phase or self.activation["start_time"] == 0.0:
                phase_record = self.load_phase_from_supabase(current_phase)
                base_duration = phase_record.get("base_duration", self.min_green) if phase_record else self.min_green
                # Estimate current total as elapsed + remaining to seed desired_total
                elapsed = self._get_phase_elapsed()
                remaining = self._get_phase_remaining()
                seed_total = max(base_duration, elapsed + remaining)
                self._reset_activation(current_phase, base_duration, seed_total)

            # Apply extension relative to base for this activation (no redundant appends)
            desired_total = self.apply_extension_delta(delta_t, buffer=0.3)
            elapsed = self._get_phase_elapsed()
            remaining = self._get_phase_remaining()
            new_total = max(desired_total or 0.0, elapsed + remaining)
            extended_time = max(0.0, new_total - (self.activation["base_duration"] or self.min_green))
            self.last_extended_time = extended_time

            logger.info(f"\n[PHASE ADJUST PATCHED] Phase {current_phase}: desired_total={desired_total:.1f}s, now_total≈{new_total:.1f}s (Δt={delta_t:.1f}s, extended≈{extended_time:.1f}s)")
            logger.info(f"  Weights: {self.weights}, Bonus: {getattr(self, 'last_bonus', 0)}, Penalty: {getattr(self, 'last_penalty', 0)}")
            return new_total
        except traci.TraCIException as e:
            logger.info(f"[ERROR] Duration adjustment failed: {e}")
            return traci.trafficlight.getPhaseDuration(self.tls_id)   
    def apply_extension_delta(self, delta_t, buffer=0.5):

        if self.activation["phase_idx"] is None:
            return None
        base = self.activation["base_duration"] if self.activation["base_duration"] is not None else self.min_green
        desired_total = float(np.clip(base + float(delta_t), self.min_green, self.max_green))
        if self._phase_has_low_current_demand(min_total_halted=self.low_demand_min_halted):
            elapsed = self._get_phase_elapsed()
            # Allow only a short tail if demand collapsed
            desired_total = min(desired_total, elapsed + self.low_demand_extend_cap)
        self._maybe_update_phase_remaining(desired_total, buffer=0.3)        
        return desired_total
    def _maybe_update_phase_remaining(self, desired_total, buffer=0.5):

        if self.activation["phase_idx"] is None:
            return False

        elapsed = self._get_phase_elapsed()
        remaining = self._get_phase_remaining()
        desired_remaining = max(0.0, float(desired_total) - elapsed)

        # Only adjust if we're meaningfully different from what's already scheduled
        if abs(remaining - desired_remaining) > float(buffer):
            try:
                traci.trafficlight.setPhaseDuration(self.tls_id, desired_remaining)
                # Update book-keeping to reflect new target
                self.activation["desired_total"] = float(desired_total)
                # Update records for auditability
                current_phase = traci.trafficlight.getPhase(self.tls_id)
                total_after_update = elapsed + desired_remaining
                phase_record = self.load_phase_from_supabase(current_phase)
                base = phase_record.get("base_duration", self.min_green) if phase_record else self.min_green
                extended_time = max(0.0, total_after_update - base)
                self.update_phase_duration_record(current_phase, total_after_update, extended_time)
                if hasattr(self, "log_phase_to_event_log"):
                    self.log_phase_to_event_log(current_phase, total_after_update)

                logger.info(f"[PATCH][EXT] Phase {current_phase}: elapsed={elapsed:.1f}s, remaining {remaining:.1f}s → {desired_remaining:.1f}s (desired_total={desired_total:.1f}s)")
                return True
            except Exception as e:
                logger.info(f"[PATCH][EXT][ERROR] Failed to set remaining time: {e}")
                return False
        return False
    def calculate_adaptive_duration(self, phase_idx):
        """Calculate phase duration based on actual demand"""
        base_duration = self.min_green
        
        # Get total queue for this phase
        queue_total = self._phase_green_total_queue(phase_idx)
        
        if queue_total == 0:
            # Minimum time only for empty phases
            return max(3, self.min_green // 3)  # Very short
        elif queue_total < 3:
            # Short time for low demand
            return self.min_green
        else:
            # Normal calculation for busy phases
            return min(self.max_green, self.min_green + queue_total * 2)
    def check_phase_termination(self, phase_idx):
        """Check if current phase should terminate early"""
        elapsed = self._get_phase_elapsed()
        
        # Don't terminate before minimum green
        if elapsed < self.min_green:
            return False
        
        # Check if all green lanes are empty
        if not self._phase_has_demand(phase_idx):
            # Give 2-3 seconds gap time for approaching vehicles
            gap_time = 3.0
            if elapsed > self.min_green + gap_time:
                return True  # Terminate early
        
        return False
    def calculate_optimal_green_time(self, lane_id):
        """Calculate optimal green time based on queue and downstream capacity"""
        try:
            queue = traci.lane.getLastStepHaltingNumber(lane_id)
            downstream_capacity = self.get_downstream_capacity(lane_id)
            
            clearance_time = queue * 2.0
            downstream_limit = downstream_capacity * 2.0
            
            optimal_time = min(
                clearance_time,
                downstream_limit * 2.0,
                self.max_green
            )
            
            arrival_rate = self._calculate_arrival_rate(lane_id)
            optimal_time += arrival_rate * 5
            
            return max(self.min_green, optimal_time)
        except Exception as e:
            self.logger.info(f"Error calculating optimal green time: {e}")
            return self.min_green
    def adapt_cycle_length(self):
        """Dynamically adjust cycle length based on demand"""
        try:
            total_demand = sum(
                traci.lane.getLastStepHaltingNumber(lane)
                for lane in self.lane_ids
            )
            
            saturation = total_demand / max(len(self.lane_ids) * 10, 1)
            
            if saturation > 0.9:
                new_cycle = min(180, self.base_cycle * 1.5)
            elif saturation < 0.3:
                new_cycle = max(60, self.base_cycle * 0.7)
            else:
                new_cycle = self.base_cycle
            
            self.cycle_length = new_cycle
            
            self._log_apc_event({
                "action": "cycle_length_adapted",
                "new_cycle": new_cycle,
                "saturation": saturation
            })
        except Exception as e:
            self.logger.info(f"Error adapting cycle length: {e}")
    def get_current_extension_seconds(self):
        try:
            if self.activation["phase_idx"] is None:
                return 0.0
            elapsed = self._get_phase_elapsed()
            remaining = self._get_phase_remaining()
            base = self.activation.get("base_duration") or self.min_green
            return max(0.0, (elapsed + remaining) - base)
        except Exception:
            return 0.0
    def emit_extension_telemetry(self, threshold=0.5):
        try:
            if self.activation["phase_idx"] is None:
                return 0.0
            elapsed = self._get_phase_elapsed()
            remaining = self._get_phase_remaining()
            base = self.activation.get("base_duration") or self.min_green
            total = elapsed + remaining
            extended = max(0.0, total - base)
            # throttle event spam
            if self._last_ext_telemetry < 0 or abs(extended - self._last_ext_telemetry) >= threshold:
                self._last_ext_telemetry = extended
                if hasattr(self, "controller") and hasattr(self.controller, "phase_events"):
                    self.controller.phase_events.append({
                        "tls_id": self.tls_id,
                        "phase_idx": self.activation.get("phase_idx"),
                        "base_duration": base,
                        "duration": total,
                        "extended_time": extended,
                        "timestamp": datetime.datetime.now().isoformat(),
                        "telemetry": True  # marker so the UI can distinguish from patch events
                    })
            return extended
        except Exception:
            return 0.0
    def _get_phase_elapsed(self):
        try:
            return max(0.0, traci.simulation.getTime() - float(self.activation.get("start_time", 0.0)))
        except Exception:
            return 0.0
    def _get_phase_remaining(self):
        try:
            now = traci.simulation.getTime()
            next_switch = traci.trafficlight.getNextSwitch(self.tls_id)
            return max(0.0, next_switch - now)
        except Exception:
            return 0.0
    def log_phase_adjustment(self, action_type, phase, old_duration, new_duration):
        logger.info(f"[LOG] {action_type} phase {phase}: {old_duration} -> {new_duration}")    
    # ========================================
    # 6. REQUEST QUEUE MANAGEMENT
    # ========================================    
    def request_phase_change(self, phase_idx, priority_type='normal', extension_duration=None):
        phase_idx = self._safe_phase_index(int(phase_idx), force_reload=True)
        if phase_idx is None:
            self.logger.info(f"[REQUEST] {self.tls_id}: ignoring request, no valid phases")
            return False

        current_time = traci.simulation.getTime()
        priority_order = {
            'protected_left': 11,
            'emergency': 10,
            'critical_starvation': 9,
            'heavy_congestion': 8,
            'starvation': 5,
            'normal': 1
        }

        req = {
            "phase_idx": int(phase_idx),
            "priority": int(priority_order.get(priority_type, 1)),
            "priority_type": str(priority_type),
            "extension_duration": None if extension_duration is None else float(extension_duration),
            "timestamp": float(current_time)
        }

        # De-duplicate exact same phase/priority pair; keep earliest (stable FIFO within priority)
        for r in self.pending_requests:
            if r["phase_idx"] == req["phase_idx"] and r["priority_type"] == req["priority_type"]:
                if req["extension_duration"] and (not r["extension_duration"] or req["extension_duration"] > r["extension_duration"]):
                    r["extension_duration"] = req["extension_duration"]
                return True

        self.pending_requests.append(req)
        self.pending_requests.sort(key=lambda x: (-x["priority"], x["timestamp"]))

        self._log_apc_event({
            "action": "pending_phase_request",
            "requested_phase": phase_idx,
            "priority_type": priority_type,
            "extension_duration": extension_duration,
            "current_phase": traci.trafficlight.getPhase(self.tls_id),
            "stack_len": len(self.pending_requests),
            "timestamp": current_time
        })

        if self.is_phase_ending():
            top = self.pending_requests[0] if self.pending_requests else None
            if top and top["priority_type"] in ['protected_left', 'emergency', 'critical_starvation']:
                return self.process_pending_requests_on_phase_end()
        return True 
    def process_pending_requests_on_phase_end(self):
        if not self.pending_requests:
            return False

        current_time = traci.simulation.getTime()
        current_phase = traci.trafficlight.getPhase(self.tls_id)
        elapsed = current_time - self.last_phase_switch_sim_time

        best_phase, best_ext = self.select_best_phase_from_requests()
        if best_phase is None:
            return False

        # Re-validate best_phase against current TLS program
        best_phase = self._safe_phase_index(best_phase)
        if best_phase is None:
            return False

        highest_ptype = 'normal'
        highest_p = -1
        for r in self.pending_requests:
            if r["phase_idx"] == best_phase and r["priority"] > highest_p:
                highest_p = r["priority"]
                highest_ptype = r["priority_type"]

        if elapsed < self.min_green and highest_ptype not in ['protected_left', 'emergency']:
            logger.info(f"[PENDING REQUEST BLOCKED] {self.tls_id}: Min green ({elapsed:.1f}s) < {self.min_green}s")
            return False

        ext = best_ext if best_ext is not None else self.min_green
        logger.info(f"[EXECUTING STACKED REQUEST] {self.tls_id}: -> phase {best_phase} (ptype={highest_ptype}, ext={ext})")
        success = self.set_phase_from_API(best_phase, requested_duration=ext)
        if success:
            served = [r for r in self.pending_requests if r["phase_idx"] == best_phase]
            self.pending_requests = [r for r in self.pending_requests if r["phase_idx"] != best_phase]

            self._log_apc_event({
                "action": "executed_stacked_requests",
                "old_phase": current_phase,
                "new_phase": best_phase,
                "priority_type": highest_ptype,
                "extension_duration": ext,
                "served_count": len(served),
                "remaining_count": len(self.pending_requests),
                "max_request_age": (current_time - min(r["timestamp"] for r in served)) if served else 0.0
            })
            return True

        logger.info(f"[PENDING REQUEST FAILED] {self.tls_id}: Failed to execute stacked change")
        return False
    def select_best_phase_from_requests(self):
        if not self.pending_requests:
            return None, None

        # Gather candidate phases present in the stack
        candidate_indices = sorted(set(r["phase_idx"] for r in self.pending_requests))
        best = None
        best_score = -1
        best_earliest = float('inf')
        best_ext = None

        for idx in candidate_indices:
            score, ext, earliest_ts = self._score_phase_from_pending(idx)
            if score > best_score or (score == best_score and earliest_ts < best_earliest):
                best = idx
                best_score = score
                best_ext = ext
                best_earliest = earliest_ts

        return best, best_ext
    def clear_pending_requests(self):
        logger.info(f"[PENDING REQUEST CLEARED] {self.tls_id}: Cleared {len(self.pending_requests)} pending requests")
        self.pending_requests.clear()
    def _score_phase_from_pending(self, phase_idx):
        score = 0
        best_ext = None
        earliest_ts = float('inf')
        for r in self.pending_requests:
            if r["phase_idx"] == phase_idx:
                score += r["priority"]
                if r["extension_duration"] is not None:
                    best_ext = max(best_ext or 0.0, r["extension_duration"])
                earliest_ts = min(earliest_ts, r["timestamp"])
        return score, (best_ext if best_ext is not None else None), earliest_ts
    def get_pending_request_status(self):
        now_ts = traci.simulation.getTime()
        return {
            "stack_size": len(self.pending_requests),
            "top_request": self.pending_requests[0] if self.pending_requests else None,
            "all_requests": [dict(r) for r in self.pending_requests[:10]],  # capped for readability
            "phase_ending": self.is_phase_ending(),
            "age_top": (now_ts - self.pending_requests[0]["timestamp"]) if self.pending_requests else 0.0
        }
    # ========================================
    # 7. EMERGENCY & PRIORITY HANDLING
    # ========================================    
    def check_special_events(self):
        now = traci.simulation.getTime()
        if hasattr(self, "_last_special_check") and now - self._last_special_check < 1:  # Reduced from 2
            return None, None
        self._last_special_check = now
        next_switch = traci.trafficlight.getNextSwitch(self.tls_id)
        time_left = max(0, next_switch - now)
        for lane_id in self.lane_ids:
            for vid in traci.lane.getLastStepVehicleIDs(lane_id):
                try:
                    v_type = traci.vehicle.getTypeID(vid)
                    key = (lane_id, vid)
                    last_evt_time = self.last_emergency_event.get(key, -9999)
                    # Only log if new or enough time has passed
                    if ('emergency' in v_type or 'priority' in v_type and
                        now - last_evt_time > self.min_green):
                        self._log_apc_event({
                            "action": "emergency_vehicle",
                            "lane_id": lane_id,
                            "vehicle_id": vid,
                            "vehicle_type": v_type
                        })
                        self.last_emergency_event[key] = now
                        self.emergency_cooldown[lane_id] = now
                        self.emergency_global_cooldown = now
                        return 'emergency_vehicle', lane_id
                except traci.TraCIException:
                    continue

        if now - self.severe_congestion_global_cooldown < self.severe_congestion_global_cooldown_time:
            return None, None

        congested_lanes = []
        for lane_id in self.lane_ids:
            if now - self.severe_congestion_cooldown.get(lane_id, 0) < self.min_green / 2:  # Reduced from full min_green
                continue
            queue, _, _, _ = self.get_lane_stats(lane_id)
            if queue >= self.severe_congestion_threshold * 10:
                congested_lanes.append((lane_id, queue))

        if congested_lanes:
            lane_id, queue = max(congested_lanes, key=lambda x: x[1])
            self.severe_congestion_cooldown[lane_id] = now
            self.severe_congestion_global_cooldown = now
            return 'severe_congestion', lane_id
        return None, None
    def check_priority_conditions(self):
        # Returns True if there is a priority event that allows preemption of min green
        # You may want to expand this as needed (emergency, protected left, etc)
        event_type, event_lane = self.check_special_events()
        if event_type == "emergency_vehicle":
            return True
        if self.serve_true_protected_left_if_needed():
            return True
        return False
    def emergency_rebalance_phases(self):
        """Emergency rebalancing when lanes are severely imbalanced"""
        try:
            current_time = traci.simulation.getTime()
            
            # Count empty vs busy lanes
            empty_lanes = []
            busy_lanes = []
            critical_lanes = []
            
            for lane in self.lane_ids:
                veh_count = traci.lane.getLastStepVehicleNumber(lane)
                queue = traci.lane.getLastStepHaltingNumber(lane)
                
                if veh_count == 0:
                    empty_lanes.append(lane)
                elif queue > 10:
                    critical_lanes.append((lane, queue))
                elif queue > 5:
                    busy_lanes.append((lane, queue))
            
            # If we have critical imbalance, force immediate action
            if critical_lanes and len(empty_lanes) > len(self.lane_ids) * 0.5:
                # Sort by queue length
                critical_lanes.sort(key=lambda x: x[1], reverse=True)
                worst_lane, worst_queue = critical_lanes[0]
                
                self.logger.warning(f"[EMERGENCY REBALANCE] {self.tls_id}: "
                                f"{len(empty_lanes)} empty, {len(critical_lanes)} critical")
                
                # Find or create phase for worst lane
                phase = self.find_or_create_phase_for_lane(worst_lane)
                if phase is not None:
                    # Give it substantial time to clear
                    duration = min(60, max(30, worst_queue * 2))
                    self.set_phase_from_API(phase, requested_duration=duration)
                    self.logger.info(f"[REBALANCE] Activated phase {phase} for {worst_lane} "
                                f"(queue={worst_queue}) for {duration}s")
                    return True
            
            return False
        except Exception as e:
            self.logger.error(f"Emergency rebalance failed: {e}")
            return False
    # ========================================
    # 8. PROTECTED LEFT TURN LOGIC
    # ========================================
    def get_protected_left_lanes(self):
        protected_lefts = []
        controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
        for lane_id in controlled_lanes:
            for link in traci.lane.getLinks(lane_id):
                if link[6] == 'l':
                    protected_lefts.append(lane_id)
                    break
        return protected_lefts
    def get_conflicting_straight_lanes(self, left_lane):
        controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
        conflicting_lanes = []
        
        try:
            # Get the edge this left lane is on
            left_edge = traci.lane.getEdgeID(left_lane)
            
            # For each controlled lane, check if it conflicts
            for lane in controlled_lanes:
                if lane == left_lane:
                    continue
                    
                lane_edge = traci.lane.getEdgeID(lane)
                
                # Simple heuristic: lanes from different edges that aren't left turns
                # are potential conflicts
                if lane_edge != left_edge:
                    links = traci.lane.getLinks(lane)
                    is_left = any(len(link) > 6 and link[6] == 'l' for link in links)
                    
                    if not is_left:  # Straight or right turn lanes can conflict
                        conflicting_lanes.append(lane)
                        
        except Exception as e:
            logger.info(f"[ERROR] Conflict detection failed: {e}")

        return conflicting_lanes
    def is_in_protected_left_phase(self):
        logic = self._get_logic()
        current_phase = traci.trafficlight.getPhase(self.tls_id)
        controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
        if current_phase >= len(logic.getPhases()):
            return None, None
        phase_state = logic.getPhases()[current_phase].state
        # Protected left: only one lane green, rest red, and that lane must be a left lane
        green_indices = [i for i, s in enumerate(phase_state) if s.upper() == 'G']
        if len(green_indices) == 1:
            lane_id = controlled_lanes[green_indices[0]]
            links = traci.lane.getLinks(lane_id)
            if any(len(link) > 6 and link[6] == 'l' for link in links):
                return lane_id, current_phase
        return None, None
    def step_extend_protected_left_if_blocked(self):

        lane_id, phase_idx = self.is_in_protected_left_phase()
        if lane_id is None:
            return False
            
        # Check if still blocked
        vehicles = traci.lane.getLastStepVehicleIDs(lane_id)
        if not vehicles:
            return False
            
        # All vehicles stopped? Check speed and waiting time
        speeds = [traci.vehicle.getSpeed(vid) for vid in vehicles]
        front_vehicle = vehicles[0]
        stopped_time = traci.vehicle.getAccumulatedWaitingTime(front_vehicle)
        
        # FIXED: Check if blockage persists
        if max(speeds) < 0.2 and stopped_time > 5:
            # Still blocked - extend up to max_green total for this activation
            desired_total = float(self.max_green)
            # Ensure activation is aligned (if we just entered this phase without activation state)
            if self.activation["phase_idx"] != phase_idx:
                pr = self.load_phase_from_supabase(phase_idx)
                base_dur = pr.get("base_duration", self.min_green) if pr else self.min_green
                self._reset_activation(phase_idx, base_dur, desired_total)
            # Only update remaining if needed (no redundant appends)
            changed = self._maybe_update_phase_remaining(desired_total, buffer=0.5)
            if changed:
                current_phase = traci.trafficlight.getPhase(self.tls_id)
                elapsed = self._get_phase_elapsed()
                remaining = self._get_phase_remaining()
                total_now = elapsed + remaining
                base_d = self.activation["base_duration"] or self.min_green
                extended_time = max(0.0, total_now - base_d)
                logger.info(f"[FIXED EXTEND/PATCH] Protected left phase {phase_idx} for lane {lane_id}: total≈{total_now:.1f}s (extended≈{extended_time:.1f}s)")
                self._log_apc_event({
                    "action": "extend_protected_left_active",
                    "lane_id": lane_id,
                    "phase": phase_idx,
                    "new_duration": total_now,
                    "extended_time": extended_time
                })
                return True
        # Not blocked anymore, should switch to a different phase
        elif traci.simulation.getTime() - self.last_phase_switch_sim_time > self.min_green:
            # Find next phase that serves vehicles
            best_phase = self.find_best_phase_for_traffic()
            if best_phase is not None and best_phase != phase_idx:
                logger.info(f"[FIXED PHASE SWITCH] Protected left no longer needed, switching to phase {best_phase}")
                self.request_phase_change(best_phase, priority_type="normal")
                return True
                
        return False    
    def detect_blocked_left_turn_with_conflict(self):
        logger.info(f"[DEBUG] Checking left-turn lanes for blockage...")
        try:
            controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
            current_time = traci.simulation.getTime()
            current_phase_idx = traci.trafficlight.getPhase(self.tls_id)
            logic = self._get_logic()
            if current_phase_idx >= len(logic.getPhases()):
                logger.info(f"[DEBUG] Invalid phase index {current_phase_idx}, skipping left-turn check")
                self._decay_blocked_memory()
                return None, False

            phase_state = logic.getPhases()[current_phase_idx].state
            left_turn_candidates = []

            for lane_idx, lane_id in enumerate(controlled_lanes):
                links = traci.lane.getLinks(lane_id)
                is_left = any(len(link) > 6 and link[6] == 'l' for link in links)
                if not is_left:
                    continue

                # Any controlled link of this lane green?
                try:
                    controlled_links = traci.trafficlight.getControlledLinks(self.tls_id)
                    idxs = [i for i, lk in enumerate(controlled_links) if lk[0][0] == lane_id]
                    is_green = any((i < len(phase_state) and phase_state[i].upper() == 'G') for i in idxs)
                except Exception:
                    is_green = False

                queue, waiting_time, mean_speed, density = self.get_lane_stats(lane_id)
                vehicles = traci.lane.getLastStepVehicleIDs(lane_id)
                has_vehicles = len(vehicles) > 0

                if not has_vehicles:
                    logger.info(f"[DEBUG] Left lane {lane_id}: No vehicles present")
                    continue
                if not is_green:
                    logger.info(f"[DEBUG] Left lane {lane_id}: Not currently green")
                    continue

                queue_threshold = self.protected_left_min_queue                
                speed_threshold = 2.0
                density_threshold = 0.08

                if queue < queue_threshold:
                    logger.info(f"[DEBUG] Left lane {lane_id}: Queue {queue} below threshold {queue_threshold}")
                    continue

                speed_blocked = mean_speed < speed_threshold
                density_blocked = density > density_threshold

                if speed_blocked or density_blocked:
                    trigger_reason = []
                    if speed_blocked:
                        trigger_reason.append(f"speed criteria: {mean_speed:.2f} < {speed_threshold}")
                    if density_blocked:
                        trigger_reason.append(f"density criteria: {density:.3f} > {density_threshold}")
                    trigger_description = " AND ".join(trigger_reason)
                    logger.info(f"[DEBUG] Left lane {lane_id}: BLOCKED ({trigger_description} AND queue={queue} >= {queue_threshold})")

                    # Debounce counter already handled outside; we only look for conflicts
                    conflicting_lanes = self.get_conflicting_straight_lanes(lane_id)
                    has_conflict = any(
                        (self.is_lane_green(conf_lane) and traci.lane.getLastStepVehicleNumber(conf_lane) > 0)
                        for conf_lane in conflicting_lanes
                    )

                    if has_conflict:
                        # Memory increment for learning and guard
                        self.blocked_left_memory[lane_id] = min(self.blocked_left_memory.get(lane_id, 0) + 1, 100)
                        left_turn_candidates.append((lane_id, queue, mean_speed, density, trigger_description))
                    else:
                        logger.info(f"[DEBUG] Left lane {lane_id}: Blocked but no conflicting traffic")

            if left_turn_candidates:
                # Persist guard window to bias RL for a little while
                lane_id, queue, speed, density, reason = max(left_turn_candidates, key=lambda x: x[1])
                self.blocked_focus_lane = lane_id
                # Keep guard for 2×min_green (tunable)
                self.blocked_guard_deadline = current_time + max(2*self.min_green, 15.0)
                logger.info(f"[PROTECTED LEFT SELECTED] Focus lane {lane_id}; guard until t={self.blocked_guard_deadline:.1f}")
                return lane_id, True

            # No blocked-left with conflict: decay memory gradually
            self._decay_blocked_memory()
            logger.info(f"[DEBUG] No left-turn lanes require protection")
            return None, False

        except Exception as e:
            logger.info(f"[ERROR] Enhanced left turn detection failed: {e}")
            self._decay_blocked_memory()
            return None, False
    def serve_protected_left_turn(self, left_lane):
        try:
            # Always create or find a dedicated protected left phase
            phase_idx = self.create_protected_left_phase_for_lane(left_lane)
            if phase_idx is None:
                logger.info(f"[ERROR] Could not create protected left phase for {left_lane}")
                return False

            # Dynamic green time based on queue length
            queue = traci.lane.getLastStepHaltingNumber(left_lane)
            wait = traci.lane.getWaitingTime(left_lane)
            green_duration = min(self.max_green, max(self.min_green, queue * 2 + wait * 0.1))

            # Activate via safe API
            success = self.set_phase_from_API(phase_idx, requested_duration=green_duration)
            if success:
                logger.info(f"[PROTECTED LEFT SUCCESS] Phase {phase_idx} activated for lane {left_lane} (duration: {green_duration}s)")
                return True
            else:
                logger.info(f"[PROTECTED LEFT FAILED] Could not set phase {phase_idx}")
                return False
        except Exception as e:
            logger.info(f"[ERROR] Protected left handling failed: {e}")
            traceback.logger.info_exc()
            return False
    def serve_true_protected_left_if_needed(self):
        lane_id, needs_protection = self.detect_blocked_left_turn_with_conflict()
        if not needs_protection or lane_id is None:
            return False
            
        # Get current phase and how long it's been active
        current_phase = traci.trafficlight.getPhase(self.tls_id)
        current_time = traci.simulation.getTime()
        time_in_phase = current_time - self.last_phase_switch_sim_time
        
        # CRITICAL FIX: If we've been serving the same protected left phase for too long,
        # force a change to serve other movements
        if time_in_phase > 30:  # 30 seconds max for any protected left phase
            # Find a different phase that serves heavy traffic
            next_phase = self.find_best_phase_for_traffic()
            if next_phase is not None and next_phase != current_phase:
                logger.info(f"[ROTATION] Protected left phase {current_phase} has been active for {time_in_phase:.1f}s. Rotating to phase {next_phase}")
                self.set_phase_from_API(next_phase)
                return True
        
        # Don't re-request the same phase we're already in
        phase_idx = self.create_protected_left_phase_for_lane(lane_id)
        if phase_idx is None:
            logger.info(f"[PATCH] Could not create protected left phase for {lane_id}")
            return False
            
        # IMPORTANT FIX: Don't activate the same phase we're already in
        if phase_idx == current_phase:
            # Only extend the duration if needed
            remaining_time = traci.trafficlight.getNextSwitch(self.tls_id) - current_time
            if remaining_time < 15:  # Only extend if less than 15 seconds left
                logger.info(f"[ROTATION] Already in protected left phase {phase_idx}, extending remaining to 15s")
                # Extend such that the remaining time is ~15s (i.e., total = elapsed + 15)
                desired_total = self._get_phase_elapsed() + 15.0
                self._maybe_update_phase_remaining(desired_total, buffer=0.2)            
            return True
            
        # Proceed with normal phase activation for a different phase
        queue = traci.lane.getLastStepHaltingNumber(lane_id)
        wait = traci.lane.getWaitingTime(lane_id)
        green_duration = min(self.max_green, max(self.min_green, queue * 2 + wait * 0.1))

        # PATCH: don't preempt immediately unless min green satisfied; queue high-priority if not
        elapsed = current_time - self.last_phase_switch_sim_time
        if elapsed < self.min_green:
            # queue as emergency so it preempts at the soonest safe end-of-phase
            self.request_phase_change(phase_idx, priority_type='emergency', extension_duration=green_duration)
            logger.info(f"[PATCH] Queued protected left phase {phase_idx} for lane {lane_id} (duration: {green_duration}s), elapsed={elapsed:.1f}s < min_green")
            return True

        self.set_phase_from_API(phase_idx, requested_duration=green_duration)
        logger.info(f"[PATCH] Activated protected left phase {phase_idx} for lane {lane_id} (duration: {green_duration}s)")
        return True         
    def create_protected_left_phase_for_lane(self, left_lane):
        try:
            controlled_links = traci.trafficlight.getControlledLinks(self.tls_id)
            logic = self._get_logic()

            if not controlled_links:
                logger.info(f"[ERROR] No controlled links found for {self.tls_id}")
                return None

            # Find all link indices for this left lane
            left_link_indices = [i for i, link in enumerate(controlled_links) if link[0][0] == left_lane]
            if not left_link_indices:
                logger.info(f"[ERROR] No controlled links for lane {left_lane}")
                return None

            # Build the phase state: only the left-turn links are green
            protected_state = ''.join('G' if i in left_link_indices else 'r' for i in range(len(controlled_links)))
            
            # Check for existing identical phase first
            for idx, phase in enumerate(logic.phases):
                if phase.state == protected_state:
                    logger.info(f"[PROTECTED LEFT] Existing protected left phase found at idx {idx}")
                    return self._safe_phase_index(idx, force_reload=True)
                    
            # Find a phase to overwrite
            # Exclude the current phase and any yellow phases
            exclude_indices = [
                i for i, phase in enumerate(logic.phases) 
                if 'y' in phase.state or i == traci.trafficlight.getPhase(self.tls_id)
            ]
            
            phase_to_overwrite = self.find_phase_to_overwrite(protected_state, exclude_indices)
            
            if phase_to_overwrite is not None:
                # Overwrite the selected phase
                duration = self.max_green
                success = self.overwrite_phase(phase_to_overwrite, protected_state, duration)
                if success:
                    logger.info(f"[PROTECTED LEFT] Overwrote phase {phase_to_overwrite} with protected left for {left_lane}")
                    return self._safe_phase_index(phase_to_overwrite, force_reload=True)
                # If overwrite failed, fall through to append below

            # Append a new pair (green + yellow)
            green_phase = traci.trafficlight.Phase(self.max_green, protected_state)
            yellow_state = ''.join('y' if i in left_link_indices else 'r' for i in range(len(controlled_links)))
            yellow_phase = traci.trafficlight.Phase(3, yellow_state)

            phases = list(logic.getPhases()) + [green_phase, yellow_phase]
            new_logic = traci.trafficlight.Logic(
                logic.programID, logic.type, len(phases) - 2, phases
            )
            traci.trafficlight.setCompleteRedYellowGreenDefinition(self.tls_id, new_logic)

            # Invalidate both APC and controller caches after mutation
            self._invalidate_logic_cache()
            if hasattr(self, "controller") and hasattr(self.controller, "_invalidate_logic_cache"):
                self.controller._invalidate_logic_cache(self.tls_id)

            # Re-validate the new index against the current definition before returning
            safe_new_idx = self._safe_phase_index(len(phases) - 2, force_reload=True)
            logger.info(f"[PROTECTED LEFT] Added new protected-left at idx {safe_new_idx} for lane {left_lane}")
            return safe_new_idx

        except Exception as e:
            logger.info(f"[ERROR] Exception creating protected left phase: {e}")
            import traceback
            traceback.logger.info_exc()
            return None    
    def _stacked_protected_left_handler(self, blocked_left_lane, needs_protection):
        if not needs_protection or not blocked_left_lane:
            return False

        now_ts = traci.simulation.getTime()
        cooldown_s = 8.0
        last_req = self.protected_left_cooldown.get(blocked_left_lane, 0.0)
        if now_ts - last_req < cooldown_s:
            self._dbg.log("pl-cooldown", logging.DEBUG,
                        f"[DEBUG] Protected-left request for {blocked_left_lane} suppressed (cooldown {cooldown_s}s).", 1.0)
            return False

        # Build/locate a true protected-left phase (single green on that left-turn)
        phase_idx = self.create_protected_left_phase_for_lane(blocked_left_lane)
        if phase_idx is None:
            logger.info(f"[WARNING] Could not create a protected-left phase for {blocked_left_lane}")
            # As a fallback, try any existing phase serving that lane
            phase_idx = self.find_or_create_phase_for_lane(blocked_left_lane)
            if phase_idx is None:
                return False

        current_phase = traci.trafficlight.getPhase(self.tls_id)
        if phase_idx == current_phase:
            # Already in the protected-left phase -> extend remaining time to ensure clearing
            desired_total = float(min(self.max_green, self._get_phase_elapsed() + 15.0))
            self._maybe_update_phase_remaining(desired_total, buffer=0.2)
            logger.info(f"[PROTECTED LEFT ACTIVE] Extending current protected-left phase {phase_idx} to total≈{desired_total:.1f}s")
            return True

        # Queue with highest priority and prefer long extension
        success = self.request_phase_change(
            phase_idx,
            priority_type='protected_left',
            extension_duration=self.max_green
        )
        logger.info(f"[DEBUG] Request phase change for protected left: {success}")
        if success:
            self._log_apc_event({
                "action": "protected_left_turn_activated",
                "lane_id": blocked_left_lane,
                "phase_idx": phase_idx,
                "reason": "enhanced_blockage_detection_true_protected",
                "detection_method": "combined_speed_density"
            })
            self.protected_left_cooldown[blocked_left_lane] = now_ts
            logger.info(f"[PROTECTED LEFT ACTIVATED] Lane {blocked_left_lane} queued with highest priority")
            return True
        return False
    def ensure_true_protected_left_phase(self, left_lane):
        try:
            logic = self._get_logic()
            controlled_links = traci.trafficlight.getControlledLinks(self.tls_id)
            if not logic or not controlled_links:
                return None

            # Determine which link indices originate from left_lane
            left_link_indices = [i for i, link in enumerate(controlled_links) if link and link[0] and link[0][0] == left_lane]
            if not left_link_indices:
                logger.info(f"[TRUE PROTECTED LEFT PHASE] No controlled links found for left lane {left_lane}")
                return None

            n = len(controlled_links)
            protected_state_str = ''.join('G' if i in left_link_indices else 'r' for i in range(n))
            yellow_state_str = ''.join('y' if i in left_link_indices else 'r' for i in range(n))

            # If exists, return its index
            for idx, phase in enumerate(logic.getPhases()):
                if phase.state == protected_state_str:
                    return idx

            # Otherwise, create and append
            green_phase = traci.trafficlight.Phase(self.max_green, protected_state_str)
            yellow_phase = traci.trafficlight.Phase(3, yellow_state_str)
            phases = list(logic.getPhases()) + [green_phase, yellow_phase]
            new_logic = traci.trafficlight.Logic(logic.programID, logic.type, len(phases) - 2, phases)
            traci.trafficlight.setCompleteRedYellowGreenDefinition(self.tls_id, new_logic)
            self._invalidate_logic_cache()
            logger.info(f"[TRUE PROTECTED LEFT PHASE] Added for lane {left_lane} at {self.tls_id}")
            self._log_apc_event({
                "action": "add_true_protected_left_phase",
                "lane_id": left_lane,
                "green_state": protected_state_str,
                "yellow_state": yellow_state_str,
                "phase_idx": len(phases) - 2
            })
            return len(phases) - 2
        except Exception as e:
            logger.info(f"[ERROR] ensure_true_protected_left_phase failed: {e}")
            return None
    # ========================================
    # 9. CONGESTION MANAGEMENT
    # ========================================  
    def detect_congestion_patterns(self):
        """Detect congestion and report to coordinator"""
        congestion_types = {
            'spillback': False,
            'gridlock': False,
            'arterial': False,
            'localized': False,
            'critical': False
        }
        
        max_queue = 0
        total_severity = 0
        congested_lane_count = 0
        critical_lanes = []
        
        for lane_id in self.lane_ids:
            queue_length = traci.lane.getLastStepHaltingNumber(lane_id)
            lane_length = traci.lane.getLength(lane_id)
            occupancy = traci.lane.getLastStepOccupancy(lane_id)
            severity = self.calculate_congestion_severity(lane_id)
            
            max_queue = max(max_queue, queue_length)
            total_severity += severity
            
            if severity > 0.5:
                congested_lane_count += 1
            
            # PATCH: More aggressive thresholds
            if severity > 0.7:
                critical_lanes.append((lane_id, queue_length, severity))
            
            # Spillback detection
            if queue_length > 0.5 * (lane_length / 7.5):  # Reduced from 0.6
                congestion_types['spillback'] = True
                
            # Gridlock detection
            if occupancy > 0.7:  # Reduced from 0.75
                downstream_lanes = self.get_downstream_lanes(lane_id)
                blocked_count = sum(1 for dl in downstream_lanes 
                                if traci.lane.getLastStepOccupancy(dl) > 0.5)  # Reduced from 0.6
                if blocked_count > len(downstream_lanes) * 0.25:  # Reduced from 0.3
                    congestion_types['gridlock'] = True
        
        avg_severity = total_severity / max(len(self.lane_ids), 1)
        
        # PATCH: More aggressive critical detection
        if (avg_severity > 0.6 or  # Reduced from 0.65
            congested_lane_count > len(self.lane_ids) * 0.35 or  # Reduced from 0.4
            max_queue > 40):  # Reduced from 50
            congestion_types['critical'] = True
            
            # Immediate notification to coordinator
            if hasattr(self, 'controller') and hasattr(self.controller, 'corridor'):
                corridor = self.controller.corridor
                if corridor:
                    # Force immediate response
                    logger.error(f"[CRITICAL] {self.tls_id} reporting CRITICAL congestion")
                    
                    # Create emergency cluster if not in one
                    in_cluster = False
                    for cluster in corridor._congestion_clusters:
                        if self.tls_id in cluster:
                            in_cluster = True
                            break
                    
                    if not in_cluster:
                        # Create new emergency cluster
                        corridor._congestion_clusters.append([self.tls_id])
                        corridor.coordinate_congestion_response([self.tls_id])
                    
                    # PATCH: Force immediate action for critical lanes
                    if critical_lanes and avg_severity > 0.75:
                        # Sort by queue length
                        critical_lanes.sort(key=lambda x: x[1], reverse=True)
                        worst_lane = critical_lanes[0][0]
                        worst_queue = critical_lanes[0][1]
                        
                        if worst_queue > 50:
                            phase = self.find_or_create_phase_for_lane(worst_lane)
                            if phase is not None:
                                duration = min(120, max(60, worst_queue * 2))
                                self.set_phase_from_API(phase, requested_duration=duration)
                                logger.error(f"[EMERGENCY OVERRIDE] {self.tls_id}: "
                                        f"Forced phase {phase} for lane {worst_lane} "
                                        f"(queue={worst_queue}, severity={avg_severity:.2f})")
            
            self.logger.info(f"[CRITICAL CONGESTION] Detected at {self.tls_id}: "
                            f"avg_severity={avg_severity:.2f}, "
                            f"congested_lanes={congested_lane_count}, "
                            f"max_queue={max_queue}")
        
        return congestion_types    
    def calculate_congestion_severity(self, lane_id):
        """Multi-factor congestion severity score with more aggressive thresholds"""
        try:
            queue = traci.lane.getLastStepHaltingNumber(lane_id)
            wait_time = traci.lane.getWaitingTime(lane_id)
            speed = traci.lane.getLastStepMeanSpeed(lane_id)
            max_speed = traci.lane.getMaxSpeed(lane_id)
            occupancy = traci.lane.getLastStepOccupancy(lane_id)
            lane_length = traci.lane.getLength(lane_id)
            
            # More aggressive queue ratio calculation
            queue_ratio = (queue * 7.5) / max(lane_length, 1.0)
            
            # ADJUSTED: More aggressive severity calculation
            severity = (
                0.40 * min(queue_ratio * 1.5, 1.0) +      # Increased weight and scaling
                0.30 * min(wait_time / 60, 1.0) +         # Reduced threshold from 120 to 60
                0.15 * (1 - speed / max(max_speed, 0.1)) +
                0.10 * min(occupancy * 1.2, 1.0) +        # Scale up occupancy impact
                0.05 * min((queue / 20), 1.0)             # Direct queue impact
            )
            
            # More aggressive scaling for high congestion
            if severity > 0.6:  # Lowered from 0.7
                severity = min(1.0, severity * 1.3)
            
            # Force critical if queue > 50 vehicles
            if queue > 50:
                severity = max(severity, 0.85)
                
            return severity
        except Exception as e:
            self.logger.info(f"Error calculating congestion severity: {e}")
            return 0.0
    def predict_congestion(self, lane_id, horizon=30):
        """Predict if congestion will occur in next 'horizon' seconds and preempt if needed."""
        try:
            current_queue = traci.lane.getLastStepHaltingNumber(lane_id)
            arrival_rate = self._calculate_arrival_rate(lane_id)
            departure_rate = self.calculate_departure_rate(lane_id)
            predicted_queue = current_queue + (arrival_rate - departure_rate) * float(horizon)
            lane_capacity = traci.lane.getLength(lane_id) / 7.5
            will_congest = predicted_queue > lane_capacity * 0.7
            if will_congest:
                self.request_preemptive_green(lane_id, priority='high')
            return will_congest
        except Exception:
            return False
    def activate_congestion_mode(self):
        """Switch to congestion-focused control strategy"""
        self.logger.info(f"[CONGESTION MODE] Activated for {self.tls_id}")
        
        self.min_green = 15
        self.max_green = 90
        self.alpha = 1.5
        self.weights = np.array([0.5, 0.1, 0.3, 0.1])
        self.protected_left_min_queue = 10
        self.serve_empty_greens = False
    def calculate_departure_rate(self, lane_id):
        """Approximate saturation departure rate when green (veh/s)."""
        return 0.5 if self.is_lane_green(lane_id) else 0.0
    def request_preemptive_green(self, lane_id, priority='high'):
        """Request green phase preemptively"""
        phase_idx = self.find_or_create_phase_for_lane(lane_id)
        if phase_idx is not None:
            self.request_phase_change(phase_idx, priority_type=priority)
    def get_downstream_capacity(self, lane_id):
        """Smallest available capacity among downstream lanes (veh)."""
        try:
            caps = []
            for lk in (traci.lane.getLinks(lane_id) or []):
                dl = lk[0]
                if not dl:
                    continue
                length = traci.lane.getLength(dl)
                veh = traci.lane.getLastStepVehicleNumber(dl)
                caps.append((length / 7.5) - veh)
            return max(0.0, min(caps) if caps else float('inf'))
        except Exception:
            return float('inf')
    def get_downstream_lanes(self, lane_id):
        """Return downstream lanes connected from lane_id."""
        try:
            return [lk[0] for lk in traci.lane.getLinks(lane_id) if lk and lk[0]]
        except Exception:
            return []
    # ========================================
    # 10. RL AGENT INTEGRATION
    # ========================================   
    def rl_create_or_overwrite_phase(self, state_vector, desired_green_lanes=None):
        if not hasattr(self.rl_agent, 'phase_overwrite_threshold'):
            # Initialize phase overwrite threshold (how often we overwrite vs append)
            self.rl_agent.phase_overwrite_threshold = 0.7
        
        # If no specific green lanes provided, use RL agent to determine them
        if desired_green_lanes is None:
            # Get current traffic conditions
            controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
            traffic_scores = []
            
            for lane in controlled_lanes:
                queue, wait, _, _ = self.get_lane_stats(lane)
                # Score based on queue length and waiting time
                score = queue * 0.7 + min(wait / 10, 5)
                traffic_scores.append((lane, score))
            
            # Select top lanes with highest scores
            traffic_scores.sort(key=lambda x: x[1], reverse=True)
            num_lanes = min(3, max(1, len(traffic_scores)))
            desired_green_lanes = [lane for lane, _ in traffic_scores[:num_lanes]]
        
        # Create the new phase state
        new_state = self.create_phase_state(green_lanes=desired_green_lanes)
        
        # Count current phases
        logic = self._get_logic()
        phase_count = len(logic.phases)
        max_phases = 12  # SUMO phase limit
        
        # Calculate new duration based on traffic
        total_queue = sum(self.get_lane_stats(lane)[0] for lane in desired_green_lanes)
        total_wait = sum(self.get_lane_stats(lane)[1] for lane in desired_green_lanes)
        
        duration = np.clip(
            self.min_green + total_queue * 1.5 + total_wait * 0.1,
            self.min_green,
            self.max_green
        )
        
        # Check if we're near the phase limit or randomly decide to overwrite
        if phase_count >= max_phases - 1 or np.random.random() < self.rl_agent.phase_overwrite_threshold:
            # Find a suitable phase to overwrite
            phase_to_overwrite = self.find_phase_to_overwrite(new_state)
            
            if phase_to_overwrite is not None:
                # Overwrite the phase
                success = self.overwrite_phase(phase_to_overwrite, new_state, duration)
                
                if success:
                    self._log_apc_event({
                        "action": "rl_overwrite_phase",
                        "phase_idx": phase_to_overwrite,
                        "green_lanes": desired_green_lanes,
                        "new_state": new_state,
                        "duration": duration
                    })
                    
                    # Adjust overwrite threshold - increase it slightly if successful
                    self.rl_agent.phase_overwrite_threshold = min(
                        0.9, 
                        self.rl_agent.phase_overwrite_threshold + 0.02
                    )
                    
                    return phase_to_overwrite
        
        # Fall back to creating a new phase if overwriting didn't work or wasn't chosen
        # (This will use your existing methods that append phases)
        try:
            # Use existing create_or_extend_phase but with a check for max phases
            if phase_count < max_phases - 1:
                new_phase_idx = self.create_or_extend_phase(desired_green_lanes, 0)
                
                if new_phase_idx is not None:
                    # Decrease overwrite threshold slightly when we append
                    self.rl_agent.phase_overwrite_threshold = max(
                        0.5, 
                        self.rl_agent.phase_overwrite_threshold - 0.01
                    )
                    
                    return new_phase_idx
            
            # If we've reached the limit, force an overwrite of the least used phase
            logger.info("[PHASE LIMIT] Reached maximum phases, forcing phase overwrite")
            phase_to_overwrite = self.find_phase_to_overwrite(new_state)
            
            if phase_to_overwrite is not None:
                self.overwrite_phase(phase_to_overwrite, new_state, duration)
                return phase_to_overwrite
            else:
                # Last resort: reuse any existing phase with green for the desired lanes
                for idx, phase in enumerate(logic.phases):
                    phase_state = phase.state
                    controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
                    
                    for lane in desired_green_lanes:
                        if lane in controlled_lanes:
                            lane_idx = controlled_lanes.index(lane)
                            if lane_idx < len(phase_state) and phase_state[lane_idx].upper() == 'G':
                                logger.info(f"[FALLBACK] Using existing phase {idx} for desired green lanes")
                                return idx
                                
                # If even that failed, return phase 0
                return 0
                
        except Exception as e:
            logger.info(f"[ERROR] Failed to create or overwrite phase: {e}")
            import traceback
            traceback.logger.info_exc()
            return 0
    def set_coordinator_mask(self, mask_or_none):
        try:
            if mask_or_none is None:
                self.coordinator_phase_mask = None
            else:
                self.coordinator_phase_mask = list(mask_or_none)
        except Exception:
            self.coordinator_phase_mask = None
    def should_skip_phase(self, phase_idx):
        """Check if phase should be skipped due to no demand"""
        if not self.serve_empty_greens:
            if not self._phase_has_demand(phase_idx):
                # Check if any lane in this phase has been waiting too long
                logic = self._get_logic()
                if not logic or phase_idx >= len(logic.getPhases()):
                    return True
                
                phase_state = logic.getPhases()[phase_idx].state
                controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
                
                max_wait = 0
                for i, lane in enumerate(controlled_lanes):
                    if i < len(phase_state) and phase_state[i].upper() == 'G':
                        wait_time = traci.simulation.getTime() - self.last_served_time.get(lane, 0)
                        max_wait = max(max_wait, wait_time)
                
                # Only skip if no lane has been waiting too long
                if max_wait < self.max_green * 2:  # Reasonable threshold
                    return True
        
        return False
    def find_best_phase_for_traffic(self):

        logic = self._get_logic()
        if not logic:
            return None
        phases = list(getattr(logic, "phases", []))
        if not phases:
            return None

        controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
        # Pre-compute per-lane metrics for normalization and speed
        lane_metrics = {}
        max_local_q = 0.0
        max_local_wait = 0.0
        max_starve = 0.0
        max_ds_pressure = 0.0
        for lid in controlled_lanes:
            try:
                q = float(traci.lane.getLastStepHaltingNumber(lid))
                w = float(traci.lane.getWaitingTime(lid))
                vnum = float(traci.lane.getLastStepVehicleNumber(lid))
            except Exception:
                q, w, vnum = 0.0, 0.0, 0.0
            starve = self._lane_starvation_s(lid) if q > 0 or vnum > 0 else 0.0
            ds = self._downstream_pressure(lid)
            lane_metrics[lid] = {"q": q, "w": w, "vnum": vnum, "starve": starve, "ds": ds}
            max_local_q = max(max_local_q, q)
            max_local_wait = max(max_local_wait, w)
            max_starve = max(max_starve, starve)
            max_ds_pressure = max(max_ds_pressure, ds)

        # Avoid div-by-zero
        def nz(x): return x if x > 0 else 1.0

        current_phase = traci.trafficlight.getPhase(self.tls_id)
        best_phase = current_phase
        best_score = -1e18
        best_breakdown = None
        current_phase_score = None
        for pidx, phase in enumerate(phases):
           # Skip yellow-like phases
            if 'y' in str(phase.state):
                continue
            st = phase.state
            green_lanes = [
                lid for li, lid in enumerate(controlled_lanes)
                if li < len(st) and st[li].upper() == 'G'
            ]
            if not green_lanes:
                # no service value
                continue

            # Aggregate components
            q_vals = [lane_metrics[l]["q"] for l in green_lanes]
            w_vals = [lane_metrics[l]["w"] for l in green_lanes]
            s_vals = [lane_metrics[l]["starve"] for l in green_lanes]
            ds_vals = [lane_metrics[l]["ds"] for l in green_lanes]
            v_vals = [lane_metrics[l]["vnum"] for l in green_lanes]

            max_q = max(q_vals) if q_vals else 0.0
            sum_q = sum(q_vals)
            sum_w = sum(w_vals)
            max_starve_p = max(s_vals) if s_vals else 0.0
            sum_ds = sum(ds_vals)
            empty_greens = sum(1 for v in v_vals if v <= 0.0)

            # Normalize
            max_q_n = max_q / nz(max_local_q)
            sum_q_n = (sum_q / (len(green_lanes) * nz(max_local_q))) if green_lanes else 0.0
            sum_w_n = (sum_w / (len(green_lanes) * nz(max_local_wait))) if green_lanes else 0.0
            starve_n = max_starve_p / nz(max_starve)
            ds_n = (sum_ds / (len(green_lanes) * nz(max_ds_pressure))) if green_lanes else 0.0

            # Weighted score:
            # - Strong focus on the single worst queue to tackle bottlenecks
            # - Secondary focus on total demand (sum_q)
            # - Consider starvation
            # - Penalize releasing into blocked downstream
            # - Penalize empty greens strongly
            phase_score = (
                1.8 * max_q_n +
                0.9 * sum_q_n +
                0.4 * sum_w_n +
                0.5 * starve_n -
                0.3 * ds_n -
                0.4 * float(empty_greens)
            )

            if phase_score > best_score:
                best_score = phase_score
                best_phase = pidx
                best_breakdown = {
                    "max_q_n": round(max_q_n, 3),
                    "sum_q_n": round(sum_q_n, 3),
                    "sum_w_n": round(sum_w_n, 3),
                    "starve_n": round(starve_n, 3),
                   "ds_n": round(ds_n, 3),
                    "empty_g": int(empty_greens),
                    "green_cnt": len(green_lanes)
                }

            if pidx == current_phase:
                current_phase_score = phase_score

        # Hysteresis: only switch if improvement > margin
        if (best_phase != current_phase and current_phase_score is not None and
            best_score < current_phase_score * (1 + self.hysteresis_margin)):
            logger.info(f"[BEST PHASE+GROUP] {self.tls_id}: keeping {current_phase} (best {best_phase} gain insufficient: {best_score:.3f} vs {current_phase_score:.3f})")
            return current_phase

        logger.info(f"[BEST PHASE+GROUP] {self.tls_id}: selected {best_phase} score={best_score:.3f} breakdown={best_breakdown}")
        return best_phase
    def get_phase_priors(self):
        """Return a length-N array of non-negative scores (0..1) per phase to bias RL decisions."""
        logic = self._get_logic()
        if not logic:
            return np.zeros(1, dtype=float)
        phases = list(getattr(logic, "phases", []))
        controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
        lane_metrics = {}

        # Precompute lane metrics
        max_local_q = max_local_wait = max_starve = max_ds = 0.0
        for lid in controlled_lanes:
            try:
                q = float(traci.lane.getLastStepHaltingNumber(lid))
                w = float(traci.lane.getWaitingTime(lid))
                vnum = float(traci.lane.getLastStepVehicleNumber(lid))
            except Exception:
                q, w, vnum = 0.0, 0.0, 0.0
            starve = self._lane_starvation_s(lid) if q > 0 or vnum > 0 else 0.0
            ds = self._downstream_pressure(lid)
            lane_metrics[lid] = dict(q=q, w=w, v=vnum, s=starve, ds=ds)
            max_local_q = max(max_local_q, q); max_local_wait = max(max_local_wait, w)
            max_starve = max(max_starve, starve); max_ds = max(max_ds, ds)

        def nz(x): return x if x > 0 else 1.0

        scores = []
        for pidx, ph in enumerate(phases):
            st = ph.state
            if 'y' in st:
                scores.append(-1e3)  # strongly demote yellows
                continue
            green_lanes = [lid for li, lid in enumerate(controlled_lanes) if li < len(st) and st[li].upper() == 'G']
            if not green_lanes:
                scores.append(-1e3)
                continue
            q_vals = [lane_metrics[l]["q"] for l in green_lanes]
            w_vals = [lane_metrics[l]["w"] for l in green_lanes]
            s_vals = [lane_metrics[l]["s"] for l in green_lanes]
            ds_vals = [lane_metrics[l]["ds"] for l in green_lanes]
            v_vals = [lane_metrics[l]["v"] for l in green_lanes]

            empty_greens = sum(1 for v in v_vals if v <= 0.0)
            max_q_n = (max(q_vals) if q_vals else 0.0) / nz(max_local_q)
            sum_q_n = (sum(q_vals) / (len(green_lanes) * nz(max_local_q))) if green_lanes else 0.0
            sum_w_n = (sum(w_vals) / (len(green_lanes) * nz(max_local_wait))) if green_lanes else 0.0
            starve_n = (max(s_vals) if s_vals else 0.0) / nz(max_starve)
            ds_n = (sum(ds_vals) / (len(green_lanes) * nz(max_ds))) if green_lanes else 0.0

            score = (1.6 * max_q_n + 0.7 * sum_q_n + 0.3 * sum_w_n + 0.5 * starve_n - 1.0 * ds_n - 0.6 * float(empty_greens))
            scores.append(score)

        arr = np.array(scores, dtype=float)
        # Normalize to 0..1 for stable blending; handle all-negative by shifting
        if np.all(np.isneginf(arr)) or arr.size == 0:
            return np.zeros_like(arr)
        finite = np.isfinite(arr)
        if not finite.any():
            return np.zeros_like(arr)
        mn, mx = np.nanmin(arr[finite]), np.nanmax(arr[finite])
        if mx - mn < 1e-6:
            return np.clip(arr - mn, 0, None)
    def _build_valid_actions_mask(self, num_phases):

        num_phases = int(num_phases)
        if num_phases <= 0:
            return np.zeros(1, dtype=bool)

        phase_mask = [True] * num_phases
        now = traci.simulation.getTime()

        # 1. Guard window for protected-left focus (restrict to phases that serve the focus lane)
        if (self.blocked_focus_lane and
            now < self.blocked_guard_deadline and
            self.blocked_left_memory.get(self.blocked_focus_lane, 0) >= 2):
             serve_idxs = self._phases_serving_lane(self.blocked_focus_lane)
             if serve_idxs:
                 phase_mask = [(i in serve_idxs) for i in range(num_phases)]
        # 2 & 3. Filter out yellow phases and those with no demand
        try:
            logic = self._get_logic()
            if logic:
                phases = logic.getPhases()
                # Clamp to available phases
                usable_n = min(len(phases), num_phases)
                for i in range(usable_n):
                    st = phases[i].state
                    # Remove yellow phases
                    if 'y' in st:
                        phase_mask[i] = False
                        continue
                    # Remove phases with no demand (or no greens)
                    if not self._phase_has_demand(i):
                        phase_mask[i] = False

                # If everything got masked out so far, re-enable all
                if not any(phase_mask[:usable_n]):
                    phase_mask = [True] * num_phases
        except Exception:
            # On any failure, fall back to all True (robustness)
            phase_mask = [True] * num_phases

        # 4. Apply corridor coordinator mask (AND)
        try:
            if isinstance(getattr(self, "coordinator_phase_mask", None), (list, tuple)):
                coord = self.coordinator_phase_mask
                upto = min(len(phase_mask), len(coord))
                for i in range(upto):
                    if not coord[i]:
                        phase_mask[i] = False
                if not any(phase_mask):
                    # Avoid empty action set
                    phase_mask = [True] * num_phases
        except Exception:
            pass

        # Fit into agent's max action space (pad with False if agent expects bigger, or truncate)
        max_space = getattr(self.rl_agent, "max_action_space", num_phases)
        full = np.zeros(max_space, dtype=bool)
        upto = min(len(phase_mask), max_space)
        full[:upto] = np.array(phase_mask[:upto], dtype=bool)
        # Safety: ensure at least one action
        if not full.any():
            full[0] = True
        return full
    def assess_traffic_conditions(self):
        class DummyTrafficState:
            has_emergency = False
            emergency_lane = None
            max_queue = 0
            severe_threshold = 10000  # some big number
            most_congested_lane = None
            starvation_threshold = 10000
            def get_starved_lanes(self, threshold): return []
        return DummyTrafficState()
    # ========================================
    # 11. REWARD & METRICS CALCULATION
    # ========================================
    def calculate_reward(self, bonus=0, penalty=0):
        metrics = np.zeros(4)
        valid_lanes = 0
        MAX_VALUES = [0.2, 13.89, 300, 50]
        current_max = [
            max(0.1, max(traci.lane.getLastStepVehicleNumber(lid) / max(1, traci.lane.getLength(lid)) for lid in self.lane_ids)),
            max(5.0, max(traci.lane.getLastStepMeanSpeed(lid) for lid in self.lane_ids)),
            max(30.0, max(traci.lane.getWaitingTime(lid) for lid in self.lane_ids)),
            max(5.0, max(traci.lane.getLastStepHaltingNumber(lid) for lid in self.lane_ids))
        ]
        max_vals = [min(MAX_VALUES[i], current_max[i]) for i in range(4)]
        for lane_id in self.lane_ids:
            q, w, v, dens = self.get_lane_stats(lane_id)
            if any(val < 0 for val in (q, w, v, dens)):
                continue
            metrics += [
                min(dens, max_vals[0]) / max_vals[0],
                min(v, max_vals[1]) / max_vals[1],
                min(w, max_vals[2]) / max_vals[2],
                min(q, max_vals[3]) / max_vals[3]
            ]
            valid_lanes += 1
        if valid_lanes == 0:
            self.last_R = 0
            return 0
        avg_metrics = metrics / valid_lanes
        self.metric_history.append(avg_metrics)
        self.adjust_weights()

        # Base reward
        R = 100 * (
            -self.weights[0] * avg_metrics[0] +
            self.weights[1] * avg_metrics[1] -
            self.weights[2] * avg_metrics[2] -
            self.weights[3] * avg_metrics[3] +
            bonus - penalty
        )

        # --- NEW: blocked-left shaping ---
        try:
            current_phase = traci.trafficlight.getPhase(self.tls_id)
            logic = self._get_logic()
            controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
            serves_focus = False
            if self.blocked_focus_lane and 0 <= current_phase < len(logic.getPhases()):
                st = logic.getPhases()[current_phase].state
                if self.blocked_focus_lane in controlled_lanes:
                    idx = controlled_lanes.index(self.blocked_focus_lane)
                    if idx < len(st):
                        serves_focus = (st[idx].upper() == 'G')

            # Aggregate pressure from all remembered blocked lanes
            blocked_pressure = sum(self.blocked_left_memory.values())
            if blocked_pressure > 0:
                # Penalize if current phase does not serve focus; small bonus if it does
                if serves_focus:
                    R += min(10.0, 0.5 * blocked_pressure)
                else:
                    R -= min(60.0, 3.0 * blocked_pressure)
        except Exception:
            pass
        # ----------------------------------

        self.last_R = np.clip(R, -100, 100)
        logger.info(f"[REWARD] {self.tls_id}: R={self.last_R:.2f} (dens={avg_metrics[0]:.2f}, spd={avg_metrics[1]:.2f}, wait={avg_metrics[2]:.2f}, queue={avg_metrics[3]:.2f}) Weights: {self.weights}, Bonus: {bonus}, Penalty: {penalty}")
        return self.last_R
    def compute_reward_and_bonus(self):
        status_score = 0
        valid_lanes = 0
        metrics = np.zeros(4)
        MAX_VALUES = [0.2, 13.89, 300, 50]

        current_max = [
            max(0.1, max(traci.lane.getLastStepVehicleNumber(lid)/max(1, traci.lane.getLength(lid)) for lid in self.lane_ids)),
            max(5.0, max(traci.lane.getLastStepMeanSpeed(lid) for lid in self.lane_ids)),
            max(30.0, max(traci.lane.getWaitingTime(lid) for lid in self.lane_ids)),
            max(5.0, max(traci.lane.getLastStepHaltingNumber(lid) for lid in self.lane_ids))
        ]
        max_vals = [min(MAX_VALUES[i], current_max[i]) for i in range(4)]

        bonus, penalty = 0, 0
        for lane_id in self.lane_ids:
            queue, wtime, v, dens = self.get_lane_stats(lane_id)
            if queue < 0 or wtime < 0:
                continue

            metrics += [
                min(dens, max_vals[0]) / max_vals[0],
                min(v, max_vals[1]) / max_vals[1],
                min(wtime, max_vals[2]) / max_vals[2],
                min(queue, max_vals[3]) / max_vals[3]
            ]
            valid_lanes += 1
            status_score += min(queue, 50)/10 + min(wtime, 300)/60

            # Add congestion severity bonus
            if queue > 10:
                bonus += min(2.0, queue / 10.0)

        if valid_lanes == 0:
            avg_metrics = np.zeros(4)
            avg_status = 0
        else:
            avg_metrics = metrics / valid_lanes
            avg_status = status_score / valid_lanes

        if avg_status >= 5 * 1.25:
            penalty = 2
        elif avg_status <= 2.5:
            bonus += 1

        self.last_bonus = bonus
        self.last_penalty = penalty

        R = 100 * (
            -self.weights[0] * avg_metrics[0] +
            self.weights[1] * avg_metrics[1] -
            self.weights[2] * avg_metrics[2] -
            self.weights[3] * avg_metrics[3] +
            bonus - penalty
        )

        self.last_R = np.clip(R, -100, 100)
        return self.last_R, bonus, penalty
    def calculate_delta_t_and_penalty(self, R):
        # Raw delta-t is proportional to the reward difference
        raw_delta_t = self.alpha * (R - self.R_target)

        # Apply penalty for large adjustments
        penalty = max(0, abs(raw_delta_t) - self.large_delta_t)

        # Scale delta-t using tanh for smoothing and clip within desired range
        ext_t = 20 * np.tanh(raw_delta_t / 20)  # Increased scaling factor for smoother adjustments
        delta_t = np.clip(ext_t, -20, 20)  # Allow both positive and negative adjustments

        logger.info(f"[DEBUG] [DELTA_T_PENALTY] R={R:.2f}, R_target={self.R_target:.2f}, raw={raw_delta_t:.2f}, Δt={delta_t:.2f}, penalty={penalty:.2f}")
        return raw_delta_t, delta_t, penalty
    def adjust_weights(self, window=10):
        available = len(self.metric_history)
        if available == 0:
            self.weights = np.array([0.25] * 4)
            return
        use_win = min(window, available)
        recent = np.mean(list(self.metric_history)[-use_win:], axis=0)
        density, speed, wait, queue = recent
        speed_importance = 1 - min(speed, 1.0)
        values = np.array([
            min(density, 1.0),
            speed_importance,
            min(wait, 1.0),
            min(queue, 1.0)
        ])
        total = np.sum(values)
        self.weights = values / total if total != 0 else np.array([0.25] * 4)
        self.weight_history.append(self.weights.copy())
        logger.info(f"[ADAPTIVE WEIGHTS] {self.tls_id}: {self.weights}")
    def update_R_target(self, window=10):
        if len(self.reward_history) < window or self.phase_count % 10 != 0:
            return
        avg_R = np.mean(list(self.reward_history)[-window:])
        self.R_target = self.r_base + self.r_adjust * (avg_R - self.r_base)
        logger.info(f"\n[TARGET UPDATE] R_target={self.R_target:.2f} (avg={avg_R:.2f})")
    def calculate_delta_t(self, R):
        raw_delta_t = self.alpha * (R - self.R_target)
        delta_t = 10 * np.tanh(raw_delta_t / 20)
        logger.info(f"[DELTA_T] R={R:.2f}, R_target={self.R_target:.2f}, Δt={delta_t:.2f}")
        return np.clip(delta_t, -10, 10)
    def compute_status_and_bonus_penalty(self, status_threshold=5):
        status_score = 0
        valid_lanes = 0

        for lane_id in self.lane_ids:
            queue, wtime, _, _ = self.get_lane_stats(lane_id)
            if queue < 0 or wtime < 0:
                continue
            status_score += min(queue, 50)/10 + min(wtime, 300)/60
            valid_lanes += 1

        if valid_lanes == 0:
            self.last_bonus = 0
            self.last_penalty = 0
            return 0, 0

        avg_status = status_score / valid_lanes
        bonus, penalty = 0, 0

        if avg_status >= status_threshold * 1.25:
            penalty = 2
            logger.info(f"\n[PENALTY] Status={avg_status:.2f}")
        elif avg_status <= status_threshold / 2:
            bonus = 1
            logger.info(f"\n[BONUS] Status={avg_status:.2f}")

        logger.info(f"[BONUS/PENALTY] {self.tls_id}: Bonus={bonus}, Penalty={penalty}, AvgStatus={avg_status:.2f}")
        self.last_bonus = bonus
        self.last_penalty = penalty
        return bonus, penalty
    # ========================================
    # 12. LANE & TRAFFIC UTILITIES
    # ========================================
    def get_lane_stats(self, lane_id):
        try:
            res = traci.lane.getSubscriptionResults(lane_id) or {}
            w = float(traci.lane.getWaitingTime(lane_id))
            q = float(res.get(traci.constants.LAST_STEP_VEHICLE_HALTING_NUMBER, 
                            traci.lane.getLastStepHaltingNumber(lane_id)))
            v = float(res.get(traci.constants.LAST_STEP_MEAN_SPEED, 
                            traci.lane.getLastStepMeanSpeed(lane_id)))
            veh_num = float(res.get(traci.constants.LAST_STEP_VEHICLE_NUMBER, 
                                traci.lane.getLastStepVehicleNumber(lane_id)))
            dens = veh_num / max(1.0, traci.lane.getLength(lane_id))
            return q, w, v, dens
        except traci.TraCIException:
            return 0.0, 0.0, 0.0, 0.0
    def _phase_has_demand(self, pidx):
        """True if any green in phase pidx has vehicles or queue > 0."""
        try:
            logic = self._get_logic()
            if not logic:
                return True
            st = logic.getPhases()[pidx].state
            if 'y' in st:
                return False
            controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
            has_green = False
            for li, lid in enumerate(controlled_lanes):
                if li < len(st) and st[li].upper() == 'G':
                    has_green = True
                    if (traci.lane.getLastStepVehicleNumber(lid) > 0 or
                        traci.lane.getLastStepHaltingNumber(lid) > 0 or
                        traci.lane.getWaitingTime(lid) > 0):
                        return True
            # If a phase has no greens, treat as no-demand
            return False if has_green else False
        except Exception:
            return True  # be permissive on errors 
    def _phase_green_total_queue(self, phase_idx=None):
        """Return total halting vehicles across all green lanes for phase_idx (current if None)."""
        try:
            logic = self._get_logic()
            if not logic:
                return 0.0
            if phase_idx is None:
                phase_idx = traci.trafficlight.getPhase(self.tls_id)
            st = logic.getPhases()[phase_idx].state
            controlled = traci.trafficlight.getControlledLanes(self.tls_id)
            total = 0.0
            for i, lid in enumerate(controlled):
                if i < len(st) and st[i].upper() == 'G':
                    total += traci.lane.getLastStepHaltingNumber(lid)
            return total
        except Exception:
            return 0.0
    def _get_phase_lanes(self, phase_idx):
        """Get lanes served by a specific phase"""
        try:
            logic = self._get_logic()
            if not logic or phase_idx >= len(logic.getPhases()):
                return []
            
            phase_state = logic.getPhases()[phase_idx].state
            controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
            
            served_lanes = []
            for i, lane in enumerate(controlled_lanes):
                if i < len(phase_state) and phase_state[i].upper() == 'G':
                    served_lanes.append(lane)
            
            return served_lanes
        except Exception:
            return []
    def is_lane_green(self, lane_id):
        try:
            links = traci.trafficlight.getControlledLinks(self.tls_id)
            logic = self._get_logic()
            phase_idx = traci.trafficlight.getPhase(self.tls_id)
            if not logic or phase_idx >= len(logic.getPhases()):
                return False
            state = logic.getPhases()[phase_idx].state
            return any(i < len(state) and state[i].upper() == 'G' 
                    for i, lk in enumerate(links) if lk[0][0] == lane_id)
        except Exception:
            return False
    def _get_phase_count(self, tls_id=None):
        try:
            if tls_id is None:
                tls_id = self.tls_id
            logic = get_current_logic(tls_id)
            return len(logic.getPhases())
        except Exception as e:
            logger.info(f"[ERROR] _get_phase_count failed for {tls_id}: {e}")
            return 1  # Fallback to 1 phase (prevents crash)
    def _estimate_lane_capacity(self, lane_id):
        return max(1.0, traci.lane.getLength(lane_id) / 7.5) if hasattr(traci.lane, 'getLength') else 25.0 / 7.5
    def _downstream_pressure(self, from_lane):
        try:
            links = traci.lane.getLinks(from_lane) or []
            pressures = []
            for lk in links:
                if to_lane := lk[0]:
                    q = float(traci.lane.getLastStepHaltingNumber(to_lane))
                    veh = float(traci.lane.getLastStepVehicleNumber(to_lane))
                    cap = self._estimate_lane_capacity(to_lane)
                    pressures.append(q + 2.0 * max(0.0, veh/cap - 0.7) * cap)
            return sum(pressures) / len(pressures) if pressures else 0.0
        except Exception:
            return 0.0
    def _lane_starvation_s(self, lane_id):
        """Seconds since this lane was last served (approx)."""
        try:
            now = traci.simulation.getTime()
        except Exception:
            now = 0.0
        return max(0.0, now - float(self.last_served_time.get(lane_id, 0.0)))    
    def _phases_serving_lane(self, lane_id):
        """Return a list of phase indices that give green to any link from lane_id."""
        try:
            logic = self._get_logic()
            if not logic:
                return []
            controlled_links = traci.trafficlight.getControlledLinks(self.tls_id)
            idxs = [i for i, lk in enumerate(controlled_links) if lk and lk[0] and lk[0][0] == lane_id]
            if not idxs:
                return []
            result = []
            for pidx, ph in enumerate(logic.getPhases()):
                st = ph.state
                if any((i < len(st) and st[i].upper() == 'G') for i in idxs):
                    result.append(pidx)
            return result
        except Exception:
            return []
    def _phase_has_low_current_demand(self, phase_idx=None, min_total_halted=1):
        """True if the active (or given) phase has fewer halted vehicles than threshold."""
        return self._phase_green_total_queue(phase_idx) < float(min_total_halted)  
    def update_lane_serving_status(self):
        current_phase_idx = traci.trafficlight.getPhase(self.tls_id)
        current_time = traci.simulation.getTime()
        logic = self._get_logic()
        if current_phase_idx >= len(logic.getPhases()):
            return
        phase_state = logic.getPhases()[current_phase_idx].state
        controlled_lanes = traci.trafficlight.getControlledLanes(self.tls_id)
        for i, lane_id in enumerate(controlled_lanes):
            if i < len(phase_state) and phase_state[i].upper() == 'G':
                self.last_served_time[lane_id] = current_time
    # ========================================
    # 13. MAIN CONTROL LOOP
    # ========================================    
    def check_and_fix_lane_imbalance(self):
        """Check for and fix severe lane imbalances early in simulation"""
        # Only run in first 200 steps or when severe imbalance detected
        if self.phase_count > 200:
            # Still check for severe imbalance
            max_queue = max(traci.lane.getLastStepHaltingNumber(l) for l in self.lane_ids)
            if max_queue < 20:  # Not severe enough
                return False
        
        # Categorize lanes
        lane_status = {}
        for lane in self.lane_ids:
            q = traci.lane.getLastStepHaltingNumber(lane)
            v = traci.lane.getLastStepVehicleNumber(lane)
            is_green = self.is_lane_green(lane)
            
            if v == 0 and is_green:
                lane_status[lane] = 'empty_green'
            elif v == 0:
                lane_status[lane] = 'empty_red'
            elif q > 8:
                lane_status[lane] = 'congested'
            elif q > 3:
                lane_status[lane] = 'moderate'
            else:
                lane_status[lane] = 'light'
        
        # Check for imbalance
        empty_greens = [l for l, s in lane_status.items() if s == 'empty_green']
        congested = [l for l, s in lane_status.items() if s == 'congested']
        
        if empty_greens and congested:
            logger.error(f"[IMBALANCE] {self.tls_id}: {len(empty_greens)} empty green lanes, "
                        f"{len(congested)} congested lanes!")
            
            # Find or create phase for congested lanes
            for lane in congested:
                phase = self.find_or_create_phase_for_lane(lane)
                if phase is not None:
                    # Check this phase doesn't serve empty lanes
                    served = self._get_phase_lanes(phase)
                    if not any(l in empty_greens for l in served):
                        self.set_phase_from_API(phase, requested_duration=self.max_green)
                        logger.info(f"[FIX IMBALANCE] Activated phase {phase} for congested lane {lane}")
                        return True
            
            # If no good phase exists, create one
            if congested:
                # Create phase serving only congested lanes
                new_phase = self.add_new_phase(
                    green_lanes=congested[:3],  # Serve up to 3 congested lanes
                    green_duration=self.max_green
                )
                if new_phase is not None:
                    self.set_phase_from_API(new_phase, requested_duration=self.max_green)
                    logger.info(f"[FIX IMBALANCE] Created and activated new phase {new_phase}")
                    return True
        
        return False
    def control_step(self):
        """Main control step with corridor coordination support"""
        self.phase_count += 1
        now = traci.simulation.getTime()
        
        # EMERGENCY CHECK 1: Rebalance if severely imbalanced
        if self.phase_count % 10 == 0:  # Check every 10 steps
            if self.emergency_rebalance_phases():
                return  # Skip normal control if emergency action taken
        
        # EMERGENCY CHECK 2: Clear stale locks
        if hasattr(self, 'controller') and hasattr(self.controller, 'corridor'):
            corridor = self.controller.corridor
            if self.tls_id in corridor._priority_locks:
                lock_time = corridor._priority_locks[self.tls_id]
                if now > lock_time:
                    # Lock expired, clear it
                    del corridor._priority_locks[self.tls_id]
                    if self.tls_id in corridor._active_priorities:
                        del corridor._active_priorities[self.tls_id]
                    self.logger.info(f"[LOCK] Cleared expired lock for {self.tls_id}")
        
        # EMERGENCY CHECK 3: Prevent phase starvation
        for lane in self.lane_ids:
            time_since_served = now - self.last_served_time.get(lane, 0)
            if time_since_served > 180:  # 3 minutes max wait
                queue = traci.lane.getLastStepHaltingNumber(lane)
                if queue > 0:
                    self.logger.warning(f"[STARVATION] {lane} not served for {time_since_served}s")
                    phase = self.find_or_create_phase_for_lane(lane)
                    if phase is not None:
                        self.set_phase_from_API(phase, requested_duration=45)
                        self.last_served_time[lane] = now
                        return
        # Initialize all critical variables at the start
        try:
            logic = self._get_logic()
            current_phase = traci.trafficlight.getPhase(self.tls_id)
            num_phases = len(logic.getPhases()) if logic else 1
        except Exception as e:
            logger.error(f"[CONTROL_STEP] Failed to get initial state for {self.tls_id}: {e}")
            # Set safe defaults
            logic = None
            current_phase = 0
            num_phases = 1
        
        # Priority 1: Check for corridor coordinator override
        if hasattr(self, 'controller') and hasattr(self.controller, 'corridor'):
            corridor = self.controller.corridor
            if corridor and self.tls_id in corridor._active_responses:
                response_type = corridor._active_responses[self.tls_id]
                severity = corridor._calculate_tl_congestion_severity(self.tls_id)
                
                logger.debug(f"[CORRIDOR CONTROL] {self.tls_id}: {response_type} response (severity={severity:.2f})")
                
                # Apply coordinator-specific logic
                if response_type == "bottleneck":
                    # Check actual queue before extending
                    queue_total = self._phase_green_total_queue()
                    if queue_total < 3:  # Don't extend if no real demand
                        # Switch to phase with actual demand
                        best_phase = self.find_best_phase_for_traffic()
                        if best_phase is not None and best_phase != current_phase:
                            self.set_phase_from_API(best_phase)
                            return
                        else:
                            # Extend current phase if serving queues
                            queue_total = self._phase_green_total_queue()
                            if queue_total > 5:
                                self.apply_extension_delta(30)
                                logger.debug(f"[BOTTLENECK] {self.tls_id}: Extending phase {current_phase} (queue={queue_total})")
                            return
                            
                elif response_type == "metering":
                    # Quick cycling to meter flow to downstream bottleneck
                    time_in_phase = now - self.last_phase_switch_sim_time
                    metering_green = max(5, self.min_green * 0.7)  # Shorter greens
                    
                    if time_in_phase >= metering_green:
                        current_phase = traci.trafficlight.getPhase(self.tls_id)
                        next_phase = (current_phase + 1) % self._get_phase_count()
                        self.set_phase_from_API(next_phase, requested_duration=metering_green)
                        logger.debug(f"[METERING] {self.tls_id}: Quick cycle to phase {next_phase}")
                    return
                    
                elif response_type == "clearance":
                    # Quick cycling to clear downstream queues
                    time_in_phase = now - self.last_phase_switch_sim_time
                    clearance_green = max(3, self.min_green // 2)
                    
                    if time_in_phase >= clearance_green:
                        current_phase = traci.trafficlight.getPhase(self.tls_id)
                        next_phase = (current_phase + 1) % self._get_phase_count()
                        self.set_phase_from_API(next_phase, requested_duration=clearance_green)
                        logger.debug(f"[CLEARANCE] {self.tls_id}: Rapid cycle to phase {next_phase}")
                    return
                    
                elif response_type == "congestion":
                    # Standard congestion response with adaptive timing
                    # Don't return - let normal control handle with modified parameters
                    pass
        
        # Priority 2: IMMEDIATE CHECK for extreme queues
        max_queue = 0
        max_queue_lane = None
        for lane in self.lane_ids:
            q = traci.lane.getLastStepHaltingNumber(lane)
            if q > max_queue:
                max_queue = q
                max_queue_lane = lane
        
        # Force emergency response if queue > 60 vehicles
        if max_queue > 60:
            self.logger.info(f"[FORCE EMERGENCY] Queue {max_queue} on {max_queue_lane}")
            
            # Check if we're already serving this lane
            current_phase = traci.trafficlight.getPhase(self.tls_id)
            if not self.is_lane_green(max_queue_lane):
                # Force switch to serve this lane
                phase = self.find_or_create_phase_for_lane(max_queue_lane)
                if phase is not None:
                    duration = min(120, max(60, max_queue * 2.5))
                    self.set_phase_from_API(phase, requested_duration=duration)
                    self.logger.info(f"[FORCE EMERGENCY] Switched to phase {phase} for {duration}s")
                    return
            else:
                # Extend current phase significantly
                extension = min(60, max_queue * 1.5)
                self.apply_extension_delta(extension)
                self.logger.info(f"[FORCE EMERGENCY] Extended current phase by {extension}s")
                return
        
        # Priority 3: Ensure RL agent context
        try:
            if self.rl_agent and hasattr(self.rl_agent, "set_context"):
                self.rl_agent.set_context(self)
        except Exception:
            pass
        
        self._dbg.log("ctrl-step", logging.DEBUG, f"[DEBUG] === control_step START === at sim time {now}", 1.0)
        self.update_lane_serving_status()
        
        # Priority 4: Protected left turn check
        if self._sched.due("left_block_check", 1.0, now):
            if self.step_extend_protected_left_if_blocked():
                self._dbg.log("pl-extended", logging.DEBUG, "[DEBUG] Protected left phase extended", 1.0)
                return
        
        # Priority 5: Special events (emergency vehicles)
        event_type, event_lane = (None, None)
        if self._sched.due("special_events", 0.5, now):
            event_type, event_lane = self.check_special_events()
        
        # Priority 6: Find best phase for current traffic
        best_phase = None
        if self._sched.due("best_phase", 0.5, now):
            best_phase = self.find_best_phase_for_traffic()
            
            # PATCH: Early detection of unbalanced lane usage
            if self.phase_count < 100:  # First 100 steps
                # Check for severely unbalanced lanes
                lane_queues = {}
                empty_lanes = []
                congested_lanes = []
                
                for lane in self.lane_ids:
                    q = traci.lane.getLastStepHaltingNumber(lane)
                    veh = traci.lane.getLastStepVehicleNumber(lane)
                    lane_queues[lane] = q
                    
                    if veh == 0 and self.is_lane_green(lane):
                        empty_lanes.append(lane)
                    elif q > 5:  # Low threshold for early detection
                        congested_lanes.append((lane, q))
                
                # If we have both empty and congested lanes, force rebalancing
                if empty_lanes and congested_lanes:
                    logic = self._get_logic()
                    num_phases = len(logic.getPhases()) if logic else 1
                    #logger.warning(f"[EARLY IMBALANCE] {self.tls_id}: {len(empty_lanes)} empty lanes, "
                                #f"{len(congested_lanes)} congested lanes at step {self.phase_count}")
                    
                    # Find phase that serves the most congested lane
                    congested_lanes.sort(key=lambda x: x[1], reverse=True)
                    worst_lane = congested_lanes[0][0]
                    
                    # Try to find a phase that serves congested lanes but not empty ones
                    best_score = -1
                    for phase_idx in range(num_phases):
                        if self._phase_has_demand(phase_idx):
                            served_lanes = self._get_phase_lanes(phase_idx)
                            
                            # Score based on serving congested lanes and avoiding empty ones
                            score = 0
                            for lane, q in congested_lanes:
                                if lane in served_lanes:
                                    score += q
                            for lane in empty_lanes:
                                if lane in served_lanes:
                                    score -= 10  # Penalty for serving empty lanes
                            
                            if score > best_score:
                                best_score = score
                                best_phase = phase_idx
                    
                    if best_phase is not None and best_phase != current_phase:
                        #logger.warning(f"[EARLY REBALANCE] Switching to phase {best_phase} to balance lanes")
                        self.set_phase_from_API(best_phase, requested_duration=self.min_green)
                        return
            
        if self.step_extend_protected_left_if_blocked():
            logger.debug("[DEBUG] Protected left phase extended, skipping this step.")
            return
            
        current_time = traci.simulation.getTime()
        
        # YELLOW-LOCK WATCHDOG
        try:
            current_phase = traci.trafficlight.getPhase(self.tls_id)
            logic = self._get_logic()
            if 0 <= current_phase < len(logic.getPhases()):
                st = logic.getPhases()[current_phase].state
                if 'G' not in st.upper():
                    time_in_phase = traci.simulation.getTime() - self.last_phase_switch_sim_time
                    if time_in_phase >= YELLOW_MAX_HOLD_S:
                        best = self.find_best_phase_for_traffic()
                        if best is not None and best != current_phase:
                            self._dbg.log("yellow-lock", logging.INFO, 
                                        f"[WATCHDOG] Breaking yellow-lock: {current_phase} -> {best}", 5.0)
                            self.set_phase_from_API(best, requested_duration=self.min_green)
                            return
        except Exception:
            pass
        
        # Track time since last phase change
        time_since_change = current_time - self.last_phase_switch_sim_time
        
        # Force phase rotation after maximum time
        if time_since_change > self.max_green:
            current_phase = traci.trafficlight.getPhase(self.tls_id)
            next_phase = (current_phase + 1) % self._get_phase_count(self.tls_id)
            logger.info(f"[FORCE ROTATION] Phase {current_phase} active for {time_since_change:.1f}s. Switching to {next_phase}")
            self.set_phase_from_API(next_phase)
            return
        
        # Starvation prevention
        longest_waiting_approach = None
        longest_wait = 0
        for lane_id in self.lane_ids:
            queue = traci.lane.getLastStepHaltingNumber(lane_id)
            if queue > 0:
                lane_wait = current_time - self.last_served_time.get(lane_id, 0)
                if lane_wait > longest_wait:
                    longest_wait = lane_wait
                    longest_waiting_approach = lane_id
        
        if (longest_waiting_approach and longest_wait > 60 and
            traci.lane.getLastStepHaltingNumber(longest_waiting_approach) >= self.min_starve_queue):
            phase_for_starving = self.find_phase_for_lane(longest_waiting_approach)
            if phase_for_starving is not None:
                logger.info(f"[STARVATION] Lane {longest_waiting_approach} waited {longest_wait:.1f}s. Activating phase {phase_for_starving}")
                self.set_phase_from_API(phase_for_starving, requested_duration=self.max_green)
                return
        
        current_phase = traci.trafficlight.getPhase(self.tls_id)
        logic = self._get_logic()
        num_phases = len(logic.getPhases())
        self._dbg.log("curr-phase", logging.DEBUG, f"[DEBUG] Current phase: {current_phase}, Num phases: {num_phases}", 1.0)
        
        # Gather lane statistics
        lane_stats = [(lid, *self.get_lane_stats(lid)) for lid in self.lane_ids]
        queues = [q for _, q, _, _, _ in lane_stats]
        waits = [w for _, _, w, _, _ in lane_stats]
        max_queue = max(queues) if queues else 0
        avg_queue = np.mean(queues) if queues else 0
        max_wait = max(waits) if waits else 0
        
        # Enhanced protected left turn detection
        blocked_left_lane, needs_protection = self.detect_blocked_left_turn_with_conflict()
        logger.debug(f"[DEBUG] Blocked left lane: {blocked_left_lane}, Needs protection? {needs_protection}")
        
        if self._stacked_protected_left_handler(blocked_left_lane, needs_protection):
            return
        
        # Find most congested and starved lanes
        maxq_idx = queues.index(max_queue) if queues else -1
        congested_lane = self.lane_ids[maxq_idx] if maxq_idx >= 0 else None
        
        # Track starved lanes
        starved_lanes = []
        starve_threshold = self.max_green * 1.5
        for lid in self.lane_ids:
            time_since_served = now - self.last_served_time.get(lid, 0)
            if time_since_served > starve_threshold:
                starved_lanes.append((lid, time_since_served))
        starved_lanes.sort(key=lambda x: x[1], reverse=True)
        
        # Congestion priority check
        congestion_priority = False
        if max_queue > (1.5 * max(1, avg_queue)) and max_queue > 3:
            congestion_priority = True
        
        # Critical starvation check
        critical_starvation = False
        critically_starved_lane = None
        critical_starve_threshold = self.max_green * 3
        for lid, time_since_served in starved_lanes:
            if time_since_served > critical_starve_threshold:
                critical_starvation = True
                critically_starved_lane = lid
                logger.info(f"[CRITICAL STARVE] {lid} not served for {time_since_served}s")
                break
        
        # Prioritized handling
        if critical_starvation and critically_starved_lane is not None:
            phase_idx = self.find_or_create_phase_for_lane(critically_starved_lane)
            if phase_idx is not None:
                logger.info(f"[CRITICAL STARVATION] Forcing phase {phase_idx} for {critically_starved_lane}")
                self.set_phase_from_API(phase_idx, requested_duration=self.max_green)
                self.last_served_time[critically_starved_lane] = now
                return
                
        elif starved_lanes:
            starved_lane, _ = starved_lanes[0]
            phase_idx = self.find_or_create_phase_for_lane(starved_lane)
            if phase_idx is not None and phase_idx != current_phase:
                logger.info(f"[STARVATION FIX] Forcing phase {phase_idx} for starved lane {starved_lane}")
                self.request_phase_change(phase_idx, priority_type="emergency", extension_duration=self.max_green)
                return
                
        elif congestion_priority and congested_lane is not None:
            phase_idx = self.find_or_create_phase_for_lane(congested_lane)
            ext = int(min(self.max_green * 2, self.min_green + 2.5 * max_queue + 0.7 * max_wait))
            if phase_idx is not None and phase_idx != current_phase:
                time_remaining = traci.trafficlight.getNextSwitch(self.tls_id) - current_time
                elapsed = current_time - self.last_phase_switch_sim_time
                if elapsed >= self.min_green:
                    logger.info("[CONGESTION FIX] Preempting now for severe congestion")
                    self.set_phase_from_API(phase_idx, requested_duration=ext)
                else:
                    logger.info(f"[CONGESTION FIX] Queueing request (min green not met, elapsed={elapsed:.1f}s)")
                    self.request_phase_change(phase_idx, priority_type="heavy_congestion", extension_duration=ext)
                return
        
        # Process pending requests at phase end
        if self.is_phase_ending():
            logger.debug("[DEBUG] Phase ending, processing pending requests.")
            if self.process_pending_requests_on_phase_end():
                return
        
        # Handle special events
        if event_type == 'emergency_vehicle':
            target_phase = self.find_or_create_phase_for_lane(event_lane)
            if target_phase is not None:
                self.request_phase_change(target_phase, priority_type='emergency')
            return
        
        # RL-based control (lowest priority)
        traffic_state = self.assess_traffic_conditions()
        optimal_phase = self.rl_agent.select_optimal_phase(traffic_state)
        
        state = np.array([
            current_phase,
            num_phases,
            *[self.get_lane_stats(lane_id)[0] for lane_id in self.lane_ids],
            *[self.get_lane_stats(lane_id)[1] for lane_id in self.lane_ids],
            self.phase_count
        ])
        
        self.rl_agent.action_size = num_phases
        try:
            valid_mask = self._build_valid_actions_mask(num_phases)
            priors = self.get_phase_priors()
            action_result = self.rl_agent.get_action(
                state,
                tl_id=self.tls_id,
                action_size=num_phases,
                valid_actions_mask=valid_mask,
                prior_bias=priors,
                prior_beta=0.4
            )
            
            if isinstance(action_result, (tuple, list)):
                target_phase = int(action_result[0])
                phase_duration = action_result[1]
            else:
                target_phase = int(action_result)
                phase_duration = None

                logic = self._get_logic()
                num_phases = len(logic.getPhases()) if logic else 1

                # === PATCH: Add skip-phase check here ===
                if self.should_skip_phase(target_phase):
                    # Find next phase with demand
                    for offset in range(1, num_phases):
                        next_phase = (target_phase + offset) % num_phases
                        if not self.should_skip_phase(next_phase):
                            target_phase = next_phase
                            break
                    else:
                        # All phases empty - just advance normally
                        target_phase = (current_phase + 1) % num_phases
                
            if target_phase >= num_phases:
                logger.warning(f"[WARNING] RL agent gave out-of-bounds action: {target_phase}")
                target_phase = 0
        except Exception as e:
            logger.info(f"[RL] Fallback to round-robin due to error: {e}")
            target_phase = (current_phase + 1) % num_phases
            phase_duration = None
        
        if target_phase != current_phase:
            logger.debug(f"[RL] Requesting phase change: {current_phase} → {target_phase}")
            self.request_phase_change(target_phase, priority_type='normal', extension_duration=phase_duration)
        
        # Calculate reward and update
        reward = self.calculate_reward()
        if hasattr(self, 'prev_state') and hasattr(self, 'prev_action'):
            self.rl_agent.update_q_table(self.prev_state, self.prev_action, reward, state, tl_id=self.tls_id)
        self.prev_state = state
        self.prev_action = target_phase
        
        # Apply extensions if no pending requests
        try:
            if not self.pending_requests:
                _, delta_t, _ = self.calculate_delta_t_and_penalty(reward)
                self.apply_extension_delta(delta_t, buffer=0.3)
        except Exception as e:
            logger.debug(f"[EXTENSION] Skipped: {e}")
        
        self.reward_history.append(reward)
        if self.phase_count % 10 == 0:
            self.update_R_target()
            
        try:
            self.emit_extension_telemetry(threshold=0.5)
        except Exception:
            pass
        
        self._dbg.log("ctrl-step-end", logging.DEBUG, "[DEBUG] === control_step END ===", 1.0)

    # ========================================
    # 14. UTILITY & CLEANUP
    # ======================================
    def shutdown(self):
        self._db_writer.stop()
        self.flush_pending_supabase_writes()
    def get_full_phase_sequence(self):
        phase_records = sorted(self.apc_state.get("phases", []), key=lambda x: x["phase_idx"])
        if not phase_records:
            return [(p.state, p.duration) for p in self._phase_defs]
        return [(rec["state"], rec["duration"]) for rec in phase_records]
class EnhancedQLearningAgent:
    # ========================================
    # 1. INITIALIZATION & SETUP
    # ========================================
    def __init__(
        self, state_size, action_size, adaptive_controller,
        learning_rate=0.1, discount_factor=0.95, epsilon=0.1, epsilon_decay=0.995, min_epsilon=0.01,
        q_table_file="enhanced_q_table.pkl", mode="train", adaptive_params=None,
        max_action_space=20, optimistic_init=10.0, coordinator=None
    ):
        self.state_size = state_size
        self.max_action_space = max_action_space
        self.action_size = min(action_size, max_action_space)
        self.learning_rate = learning_rate
        self.discount_factor = discount_factor
        self.epsilon = epsilon
        self.epsilon_decay = epsilon_decay
        self.min_epsilon = min_epsilon
        self.q_table = {}
        self.training_data = []
        self.q_table_file = q_table_file
        self._loaded_training_count = 0
        self.reward_history = []
        self.mode = mode
        
        # FIXED: Properly initialize coordinator (passed as parameter or None)
        self.coordinator = coordinator
        
        # Track coordinator overrides for debugging
        self.coordinator_overrides = 0
        self.fairness_overrides = 0
        
        self.optimistic_init = optimistic_init
        self.adaptive_params = adaptive_params or {
            'min_green': 30, 'max_green': 80, 'starvation_threshold': 30, 'reward_scale': 40,
            'queue_weight': 0.6, 'wait_weight': 0.3, 'flow_weight': 0.5, 'speed_weight': 0.2, 
            'left_turn_priority': 1.2, 'empty_green_penalty': 15, 'congestion_bonus': 10,
            'severe_congestion_threshold': 10
        }
        self.severe_threshold = self.adaptive_params.get('severe_congestion_threshold', 10)
        self.adaptive_controller = adaptive_controller
        
        if mode == "eval":
            self.epsilon = 0.0
        elif mode == "adaptive":
            self.epsilon = 0.01
        logger.info(f"AGENT INIT: mode={self.mode}, epsilon={self.epsilon}, coordinator={'yes' if coordinator else 'no'}")
    def set_context(self, adaptive_controller):
        self.adaptive_controller = adaptive_controller
    # ========================================
    # 2. STATE MANAGEMENT & VALIDATION
    # ========================================
    def is_valid_state(self, state):
        arr = np.array(state)
        return (
            isinstance(state, (list, np.ndarray))
            and arr.size == self.state_size
            and not (np.isnan(arr).any() or np.isinf(arr).any())
            and (np.abs(arr) <= 100).all()
            and not np.all(arr == 0)
        )
    def _state_to_key(self, state, tl_id=None):
        try:
            arr = np.round(np.array(state), 2) if isinstance(state, (np.ndarray, list)) else state
            key = tuple(arr.tolist()) if isinstance(arr, np.ndarray) else tuple(arr)
            return (tl_id, key) if tl_id is not None else key
        except Exception:
            return (tl_id, (0,)) if tl_id is not None else (0,)
    # ========================================
    # 3. ACTION SELECTION & DECISION MAKING
    # ========================================
    def get_action(self, state, tl_id=None, action_size=None, strategy="epsilon_greedy",
                   valid_actions_mask=None, prior_bias=None, prior_beta=0.35, **kwargs):
        """
        FIXED: Properly integrated get_action with coordinator support
        """
        action_size = action_size or self.action_size
        key = self._state_to_key(state, tl_id)
        
        # Initialize Q-table for this state if needed
        if key not in self.q_table or len(self.q_table[key]) < self.max_action_space:
            arr = np.full(self.max_action_space, self.optimistic_init, dtype=float)
            if key in self.q_table and len(self.q_table[key]) > 0:
                arr[:len(self.q_table[key])] = self.q_table[key]
            self.q_table[key] = arr
        
        qs = self.q_table[key][:self.max_action_space]

        # Base mask (bounds)
        mask = np.zeros(self.max_action_space, dtype=bool)
        mask[:action_size] = True
        if valid_actions_mask is not None:
            mask &= valid_actions_mask[:self.max_action_space]

        masked_qs = np.where(mask, qs, -np.inf)

        # Optional heuristic prior blending (guide exploration away from empty greens)
        if prior_bias is not None:
            pb = np.array(prior_bias, dtype=float)
            if pb.size < self.max_action_space:
                pad = np.zeros(self.max_action_space - pb.size, dtype=float)
                pb = np.concatenate([pb, pad], axis=0)
            # Normalize 0..1
            if np.isfinite(pb).any():
                pmin, pmax = np.nanmin(pb[np.isfinite(pb)]), np.nanmax(pb[np.isfinite(pb)])
                if pmax - pmin > 1e-6:
                    pb = (pb - pmin) / (pmax - pmin)
                else:
                    pb = np.clip(pb - pmin, 0, None)
                masked_qs = masked_qs + prior_beta * np.where(mask, pb[:self.max_action_space], 0.0)

        # FIXED: Calculate the RL agent's suggested action first
        if self.mode == "train" and np.random.rand() < self.epsilon:
            # Exploration
            valid_idxs = np.where(mask)[0]
            suggested_phase = int(np.random.choice(valid_idxs)) if len(valid_idxs) > 0 else 0
        else:
            # Exploitation
            if np.all(np.isneginf(masked_qs)):
                suggested_phase = 0
            else:
                suggested_phase = int(np.argmax(masked_qs))
        
        # FIXED: Apply coordinator if available
        if self.coordinator is not None and tl_id is not None:
            original_phase = suggested_phase
            
            # Check if coordinator allows this phase
            if not self.coordinator.should_allow_phase(tl_id, suggested_phase):
                # Get coordinator's suggestion instead
                coordinator_suggestion = self.coordinator.get_next_phase(tl_id)
                logger.debug(f"[COORDINATOR] Override {tl_id}: RL wanted {suggested_phase}, using {coordinator_suggestion}")
                suggested_phase = coordinator_suggestion
                self.coordinator_overrides += 1
            
            # Enforce fairness
            final_phase = self.coordinator.enforce_phase_fairness(tl_id, suggested_phase)
            if final_phase != suggested_phase:
                logger.debug(f"[FAIRNESS] Override {tl_id}: {suggested_phase} -> {final_phase}")
                self.fairness_overrides += 1
            
            # Record the decision
            duration = self.coordinator.suggest_phase_duration(tl_id, final_phase)
            self.coordinator.record_phase_activation(tl_id, final_phase, duration)
            
            # Log override statistics periodically
            total_actions = self.coordinator_overrides + self.fairness_overrides
            if total_actions > 0 and total_actions % 100 == 0:
                override_rate = (self.coordinator_overrides + self.fairness_overrides) / total_actions
                logger.info(f"[COORDINATOR STATS] {tl_id}: {total_actions} actions, "
                          f"{override_rate:.1%} override rate "
                          f"(coord: {self.coordinator_overrides}, fair: {self.fairness_overrides})")
            
            return final_phase
        
        # No coordinator, return RL suggestion
        return suggested_phase
    def select_and_apply_phase(self, state_vector, adaptive_controller):
        apc = adaptive_controller
        tls_id = apc.tls_id

        # Determine current action space from this TLS
        try:
            from corridor_coordinator import get_current_logic
            logic = get_current_logic(tls_id)
            n_phases = len(logic.getPhases()) if logic else 1
        except Exception:
            n_phases = 1

        # Priority lanes (emergency or blocked-left) detection on THIS TLS
        controlled_lanes = traci.trafficlight.getControlledLanes(tls_id)
        priority_lanes = []

        for lane in controlled_lanes:
            vehicles = traci.lane.getLastStepVehicleIDs(lane)
            for vid in vehicles:
                try:
                    if 'emergency' in traci.vehicle.getTypeID(vid):
                        priority_lanes.append(lane)
                        break
                except:
                    pass

        is_protected_left = False
        if not priority_lanes:
            blocked_left_lane, needs_protection = apc.detect_blocked_left_turn_with_conflict()
            if needs_protection and blocked_left_lane:
                priority_lanes.append(blocked_left_lane)
                is_protected_left = True

        # If we have priority lanes, create/overwrite a phase for them
        if priority_lanes:
            if is_protected_left:
                phase_idx = apc.create_protected_left_phase_for_lane(priority_lanes[0])
            else:
                phase_idx = apc.rl_create_or_overwrite_phase(state_vector, desired_green_lanes=priority_lanes)

            if phase_idx is not None:
                # Queue to switch at safe point
                apc.request_phase_change(phase_idx, priority_type='normal', extension_duration=None)
                return phase_idx

        # No priority lanes: RL choice for THIS TLS with per-TLS action_size
        action = self.get_action(state_vector, tl_id=tls_id, action_size=n_phases)

        if isinstance(action, int) and 0 <= action < n_phases:
            phase_idx = action
        else:
            # Interpret action and create/overwrite based on queues on this TLS
            lanes_by_queue = sorted(
                controlled_lanes,
                key=lambda l: traci.lane.getLastStepHaltingNumber(l),
                reverse=True
            )
            top_lanes = lanes_by_queue[:min(3, len(lanes_by_queue))]
            phase_idx = apc.rl_create_or_overwrite_phase(state_vector, desired_green_lanes=top_lanes)

        if phase_idx is not None:
            apc.set_phase_from_API(phase_idx, requested_duration=None)
            return phase_idx

        # Fallback
        try:
            return traci.trafficlight.getPhase(tls_id)
        except Exception:
            return 0
    def select_optimal_phase(self, traffic_state):
        if getattr(traffic_state, "has_emergency", False):
            return self.get_emergency_phase(traffic_state.emergency_lane)
        severe_threshold = self.adaptive_params.get('severe_congestion_threshold', 10)
        if getattr(traffic_state, "max_queue", 0) > severe_threshold:
            return self.get_congestion_relief_phase(getattr(traffic_state, "most_congested_lane", None))
        starved_lanes = traffic_state.get_starved_lanes(self.adaptive_params.get('starvation_threshold', 30))
        if starved_lanes:
            return self.get_starvation_relief_phase(starved_lanes[0])
        return self.select_phase(traffic_state)
    def select_phase(self, traffic_state):
        try:
            state_vector = np.array([
                getattr(traffic_state, 'max_queue', 0),
                getattr(traffic_state, 'emergency_lane', -1) if getattr(traffic_state, 'has_emergency', False) else -1,
                getattr(traffic_state, 'starvation_threshold', 0),
            ])
            action = self.get_action(state_vector)
            return action
        except Exception as e:
            logger.info(f"[select_phase ERROR]: {e}")
            return 0
    # ========================================
    # 4. Q-LEARNING CORE
    # ========================================           
    def update_q_table(self, state, action, reward, next_state, tl_id=None, extra_info=None, action_size=None):
        if self.mode == "eval" or not self.is_valid_state(state) or not self.is_valid_state(next_state):
            return
        if reward is None or np.isnan(reward) or np.isinf(reward):
            return
        action_size = action_size or self.action_size
        sk, nsk = self._state_to_key(state, tl_id), self._state_to_key(next_state, tl_id)
        for k in [sk, nsk]:
            if k not in self.q_table or len(self.q_table[k]) < self.max_action_space:
                arr = np.full(self.max_action_space, self.optimistic_init)
                if k in self.q_table and len(self.q_table[k]) > 0:
                    arr[:len(self.q_table[k])] = self.q_table[k]
                self.q_table[k] = arr
        q, nq = self.q_table[sk][action], np.max(self.q_table[nsk][:self.max_action_space])
        new_q = q + self.learning_rate * (reward + self.discount_factor * nq - q)
        if not (np.isnan(new_q) or np.isinf(new_q)):
            self.q_table[sk][action] = new_q
        # Log training data for future analysis/persistence
        entry = {
            'state': state.tolist() if isinstance(state, np.ndarray) else state,
            'action': action,
            'reward': reward,
            'next_state': next_state.tolist() if isinstance(next_state, np.ndarray) else next_state,
            'q_value': self.q_table[sk][action],
            'timestamp': time.time(),
            'learning_rate': self.learning_rate,
            'epsilon': self.epsilon,
            'tl_id': tl_id,
            'adaptive_params': self.adaptive_params.copy()
        }
        if extra_info:
            entry.update({k: v for k, v in extra_info.items() if k != "reward"})
        self.training_data.append(entry)
        self._update_adaptive_parameters(reward)
    def _update_adaptive_parameters(self, performance_value):
        # Optionally update adaptive params based on performance (stub for extensibility)
        pass
    # ========================================
    # 5. PHASE CONTROL OPERATIONS
    # ========================================
    def switch_or_extend_phase(self, state, green_lanes, force_protected_left=False):
        logger.info(f"[DEBUG][RL Agent] switch_or_extend_phase with state={state}, green_lanes={green_lanes}, force_protected_left={force_protected_left}")
        R = self.adaptive_controller.calculate_reward()
        raw_delta_t, delta_t, penalty = self.adaptive_controller.calculate_delta_t_and_penalty(R)
        logger.info(f"[DEBUG][RL Agent] R={R}, delta_t={delta_t}, penalty={penalty}")
        if force_protected_left and len(green_lanes) == 1:
            phase_idx = self.adaptive_controller.create_or_extend_protected_left_phase(green_lanes[0], delta_t)
            rl_phase_type = "protected_left"
        else:
            phase_idx = self.adaptive_controller.create_or_extend_phase(green_lanes, delta_t)
            rl_phase_type = "general"
        self.adaptive_controller._log_apc_event({
            "action": "rl_phase_request",
            "rl_phase_type": rl_phase_type,
            "requested_green_lanes": green_lanes,
            "phase_idx": phase_idx,
            "delta_t": delta_t,
            "penalty": penalty,
            "state": str(state),
        })
        logger.info(f"[DEBUG][RL Agent] Will now set phase from APC API: phase_idx={phase_idx}")
        # Only use APC yellow-aware API; do not call traci.setPhase directly
        if phase_idx is not None:
            self.adaptive_controller.set_phase_from_API(phase_idx)
        return phase_idx
    def create_or_extend_protected_left_phase(self, left_lane, delta_t):
        return self.adaptive_controller.create_or_extend_protected_left_phase(left_lane, delta_t)
    def get_emergency_phase(self, emergency_lane):
        return 0
    def get_congestion_relief_phase(self, congested_lane):
        return 0
    def get_starvation_relief_phase(self, starved_lane):
        return 0        
    # ========================================
    # 6. MODEL PERSISTENCE
    # ========================================
    def load_model(self, filepath=None):
        filepath = filepath or self.q_table_file
        logger.info(f"Attempting to load Q-table from: {filepath}")
        try:
            if os.path.exists(filepath):
                with open(filepath, 'rb') as f:
                    data = pickle.load(f)
                self.q_table = {k: np.array(v) for k, v in data.get('q_table', {}).items()}
                self._loaded_training_count = len(data.get('training_data', []))
                params = data.get('params', {})
                self.learning_rate = params.get('learning_rate', self.learning_rate)
                self.discount_factor = params.get('discount_factor', self.discount_factor)
                adaptive_params = data.get('adaptive_params', {})
                logger.info(f"Loaded Q-table with {len(self.q_table)} states from {filepath}")
                if adaptive_params:
                    logger.info("📋 Loaded adaptive parameters from previous run")
                return True, adaptive_params
            logger.info("No existing Q-table, starting fresh")
            return False, {}
        except Exception as e:
            logger.info(f"Error loading model: {e}\nNo existing Q-table, starting fresh")
            return False, {}
    def save_model(self, filepath=None, adaptive_params=None):
        filepath = filepath or self.q_table_file
        try:
            if os.path.exists(filepath):
                backup = f"{filepath}.bak_{datetime.datetime.now():%Y%m%d_%H%M%S}"
                for _ in range(3):
                    try:
                        os.rename(filepath, backup)
                        break
                    except Exception as e:
                        logger.info(f"Retrying backup: {e}")
                        time.sleep(0.5)
            meta = {
                'last_updated': datetime.datetime.now().isoformat(),
                'training_count': len(self.training_data),
                'average_reward': np.mean([x.get('reward', 0) for x in self.training_data[-100:]]) if self.training_data else 0,
                'reward_components': [x.get('reward_components', {}) for x in self.training_data[-100:]]
            }
            params = {k: getattr(self, k) for k in ['state_size','action_size','learning_rate','discount_factor','epsilon','epsilon_decay','min_epsilon']}
            model_data = {
                'q_table': {k: v.tolist() for k, v in self.q_table.items()},
                'training_data': self.training_data,
                'params': params,
                'metadata': meta
            }
            if adaptive_params:
                model_data['adaptive_params'] = adaptive_params.copy()
                logger.info(f"Saving adaptive parameters: {adaptive_params}")
            with open(filepath, 'wb') as f:
                pickle.dump(model_data, f, protocol=pickle.HIGHEST_PROTOCOL)
            logger.info(f"✅ Model saved with {len(self.training_data)} training entries")
            self.training_data = []
        except Exception as e:
            logger.info(f"Error saving model: {e}")
    # ========================================
    # 7. UTILITIES & HELPERS
    # ========================================
    def update_display(self, phase_idx, new_duration):
        now = traci.simulation.getTime()
        next_switch = traci.trafficlight.getNextSwitch(self.tls_id)
        if hasattr(self, "display"):
            self.display.update_phase_duration(
                phase_idx,
                duration=new_duration,
                current_time=now,
                next_switch_time=next_switch
            )
    def calculate_total_reward(self, adaptive_R, rl_reward):
        return adaptive_R + rl_reward
    def _get_action_name(self, action):
        return {
            0: "Set Green", 1: "Next Phase", 2: "Extend Phase",
            3: "Shorten Phase", 4: "Balanced Phase"
        }.get(action, f"Unknown Action {action}")
    def get_coordinator_stats(self):
        """Return coordinator override statistics"""
        total = self.coordinator_overrides + self.fairness_overrides
        if total > 0:
            return {
                'total_actions': total,
                'coordinator_overrides': self.coordinator_overrides,
                'fairness_overrides': self.fairness_overrides,
                'override_rate': f"{(self.coordinator_overrides + self.fairness_overrides) / total:.1%}"
            }
        return {'total_actions': 0, 'override_rate': '0%'}
class UniversalSmartTrafficController:
    DILEMMA_ZONE_THRESHOLD = 12.0  # meters
    # ========================================
    # 1. INITIALIZATION & SETUP
    # ========================================
    def __init__(self, sumocfg_path=None, mode="train", config=None, logger=None):
        self.logger = logger or logging.getLogger(__name__)
        self.mode = mode
        self.step_count = 0
        self.current_episode = 0
        self.max_consecutive_left = 1
        self.subscribed_vehicles = set()
        self.left_turn_lanes = set()
        self.right_turn_lanes = set()
        self.lane_id_list = [lid for lid in traci.lane.getIDList() if not lid.startswith(":")]
        self.lane_id_to_idx = {lid: i for i, lid in enumerate(self.lane_id_list)}
        self.lane_to_tl = {}
        self.tl_action_sizes = {}
        self.pending_next_phase = {}
        self.lane_lengths = {lid: traci.lane.getLength(lid) for lid in self.lane_id_list}
        self.lane_edge_ids = {lid: traci.lane.getEdgeID(lid) for lid in self.lane_id_list}
        self.intersection_data = {}
        self._last_phase_event_flush = time.time()
        self.tl_logic_cache = {}
        self.phase_utilization = defaultdict(int)
        self.last_phase_change = {}
        self.ambulance_active = defaultdict(bool)
        self.ambulance_start_time = defaultdict(float)
        self.left_phase_counter = defaultdict(int)
        self.previous_states = {}
        self.previous_actions = {}
        self.phase_events = []
        self.phase_event_log_file = f"phase_event_log_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.pkl"
        self.lane_scores = defaultdict(float)
        self.lane_states = defaultdict(str)
        self.consecutive_states = defaultdict(int)
        self.last_arrival_time = defaultdict(lambda: 0.0)
        self.last_lane_vehicles = defaultdict(set)
        self.last_green_time = defaultdict(float)
        self.USE_CORRIDOR = False
        self.norm_bounds = {
            'queue': 20, 'wait': 60, 'speed': 13.89,
            'flow': 30, 'density': 0.2, 'arrival_rate': 5,
            'time_since_green': 120
        }
        # PATCH: Per-intersection adaptive_phase_controllers, RL agent per intersection, not global
        self.adaptive_phase_controllers = {}
        self.arterial_lanes = []
        self.cycle_time = 90
        self.congestion_mode_active = False
        
        tls_list = traci.trafficlight.getIDList()
        for tls_id in tls_list:
            lane_ids = traci.trafficlight.getControlledLanes(tls_id)
            apc = AdaptivePhaseController(
                lane_ids=lane_ids,
                tls_id=tls_id,
                alpha=1.0,
                min_green=10,
                max_green=60
            )
            apc.controller = self
            self.adaptive_phase_controllers[tls_id] = apc
        # RL agent is now per-intersection
        self.rl_agents = {}
        for tls_id in tls_list:
            n_phases = len(traci.trafficlight.getAllProgramLogics(tls_id)[0].phases)
            rl_agent = EnhancedQLearningAgent(
                state_size=12,
                action_size=n_phases,
                adaptive_controller=self.adaptive_phase_controllers[tls_id],
                mode=mode
            )
            self.rl_agents[tls_id] = rl_agent
            self.adaptive_phase_controllers[tls_id].rl_agent = rl_agent
        # Backwards compat: keep self.apc and self.rl_agent for single intersection use
        self.corridor = None
        if self.USE_CORRIDOR and len(self.adaptive_phase_controllers) > 0:
            coordinator_config = { ... }
            self.corridor = ImprovedCorridorCoordinator(self, config=coordinator_config)
            # Pass coordinator to RL agents
            for tl_id, rl_agent in self.rl_agents.items():
                rl_agent.coordinator = self.corridor
        else:
            # Ensure RL agents have no coordinator
            for tl_id, rl_agent in self.rl_agents.items():
                rl_agent.coordinator = None
        if tls_list:
            self.apc = self.adaptive_phase_controllers[tls_list[0]]
            self.rl_agent = self.rl_agents[tls_list[0]]
        else:
            self.apc = None
            self.rl_agent = None
        self.left_turn_lanes, self.right_turn_lanes = self.detect_turning_lanes()
        self.adaptive_params = {
            'min_green': 30, 'max_green': 80, 'starvation_threshold': 40,
            'reward_scale': 40, 'queue_weight': 0.6, 'wait_weight': 0.3,
            'flow_weight': 0.5, 'speed_weight': 0.2, 'left_turn_priority': 1.2,
            'empty_green_penalty': 15, 'congestion_bonus': 10
        }
        self.corridor = None
        if len(self.adaptive_phase_controllers) > 0:
            # Use improved coordinator with enhanced configuration
            coordinator_config = {
                'spillback_threshold': 0.7,  # More sensitive spillback detection
                'congestion_threshold': 0.5,  # Earlier congestion detection
                'arterial_angle_tolerance': 30,  # Degrees for arterial alignment
                'grid_angle_tolerance': 15,  # Stricter grid detection
                'dbscan_eps': 300,  # 300m clustering radius for congestion
                'dbscan_min_samples': 2,  # Minimum cluster size
                'green_wave_speed': 13.89,  # 50 km/h default progression
                'priority_horizon': 120,  # 2 minute look-ahead for priority
                'coordination_update_interval': 30,  # Update groups every 30s
            }
            
            self.corridor = ImprovedCorridorCoordinator(
                self,
                config=coordinator_config
            )
            
            # Pass coordinator to RL agents
            for tl_id, rl_agent in self.rl_agents.items():
                rl_agent.coordinator = self.corridor
            
            self.logger.info(f"[CORRIDOR] Initialized improved coordinator for "
                           f"{len(self.adaptive_phase_controllers)} intersections with enhanced algorithms")
    def initialize(self):
        # PATCH: Loop over all intersections
        self.tl_max_phases = {}
        for tl_id in traci.trafficlight.getIDList():
            phases = traci.trafficlight.getAllProgramLogics(tl_id)[0].phases
            for i, phase in enumerate(phases):
                logger.info(f"  Phase {i}: {phase.state} (duration {getattr(phase, 'duration', '?')})")
            self.tl_max_phases[tl_id] = len(phases)
            self.tl_action_sizes[tl_id] = len(phases)
        self.lane_id_to_idx = {lid: i for i, lid in enumerate(self.lane_id_list)}
        self.idx_to_lane_id = dict(enumerate(self.lane_id_list))
        for lid in self.lane_id_list:
            self.last_green_time[lid] = 0.0
        self.subscribe_lanes(self.lane_id_list)
        self.left_turn_lanes, self.right_turn_lanes = self.detect_turning_lanes()
        # Build initial topology for coordinator
        try:
            if hasattr(self, "corridor") and self.corridor:
                self.corridor.update_topology(force=True)
        except Exception:
            pass
    def initialize_controller_phases(self):
        logger.info("[PHASE GENERATION] Ensuring all lanes have serving phases...")
        
        for tls_id in traci.trafficlight.getIDList():
            apc = self.adaptive_phase_controllers[tls_id]
            controlled_lanes = traci.trafficlight.getControlledLanes(tls_id)
            logic = traci.trafficlight.getAllProgramLogics(tls_id)[0]
            
            # PATCH: Check each lane has at least one serving phase
            unserved_lanes = []
            for lane in controlled_lanes:
                has_green = False
                for phase_idx, phase in enumerate(logic.phases):
                    lane_idx = controlled_lanes.index(lane)
                    if lane_idx < len(phase.state) and phase.state[lane_idx].upper() == 'G':
                        has_green = True
                        break
                
                if not has_green:
                    unserved_lanes.append(lane)
            
            if unserved_lanes:
                logger.warning(f"[INIT] {tls_id} has {len(unserved_lanes)} unserved lanes! Creating phases...")
                
                # Create phases for unserved lanes
                for lane in unserved_lanes:
                    # Create a simple phase for this lane
                    phase_state = ['r'] * len(controlled_lanes)
                    lane_idx = controlled_lanes.index(lane)
                    phase_state[lane_idx] = 'G'
                    
                    # Also check for compatible non-conflicting movements
                    for other_idx, other_lane in enumerate(controlled_lanes):
                        if other_idx != lane_idx:
                            # Simple compatibility check (you may need more sophisticated conflict detection)
                            if not self._lanes_conflict(lane, other_lane):
                                phase_state[other_idx] = 'G'
                    
                    new_phase = traci.trafficlight.Phase(apc.min_green, ''.join(phase_state))
                    
                    # Add to logic
                    phases = list(logic.phases)
                    phases.append(new_phase)
                    new_logic = traci.trafficlight.Logic(
                        logic.programID, logic.type, logic.currentPhaseIndex, phases
                    )
                    traci.trafficlight.setCompleteRedYellowGreenDefinition(tls_id, new_logic)
                    logger.info(f"[INIT] Added phase for lane {lane}: {''.join(phase_state)}")
                    
                    # Update logic reference
                    logic = traci.trafficlight.getAllProgramLogics(tls_id)[0]    
    def detect_turning_lanes(self):
        left, right = set(), set()
        for lid in self.lane_id_list:
            for c in traci.lane.getLinks(lid):
                if (idx := 6 if len(c) > 6 else 3 if len(c) > 3 else None):
                    (left if c[idx] == 'l' else right if c[idx] == 'r' else set()).add(lid)
        return left, right
    def _init_left_turn_lanes(self):
        try:
            self.left_turn_lanes.clear()
            for lane_id in traci.lane.getIDList():
                if any((len(conn) > 6 and conn[6] == 'l') or (len(conn) > 3 and conn[3] == 'l')
                    for conn in traci.lane.getLinks(lane_id)):
                    self.left_turn_lanes.add(lane_id)
            logger.info(f"Auto-detected left-turn lanes: {sorted(self.left_turn_lanes)}")
        except Exception as e:
            logger.info(f"Error initializing left-turn lanes: {e}")
    # ========================================
    # 2. SUBSCRIPTION & VEHICLE MANAGEMENT
    # ========================================
    def subscribe_vehicles(self, vehicle_ids):
        for vid in vehicle_ids:
            try:
                traci.vehicle.subscribe(vid, [traci.constants.VAR_VEHICLECLASS])
            except traci.TraCIException:
                pass
    def subscribe_lanes(self, lane_ids):
        for lid in lane_ids:
            traci.lane.subscribe(lid, [
                traci.constants.LAST_STEP_VEHICLE_NUMBER,
                traci.constants.LAST_STEP_MEAN_SPEED,
                traci.constants.LAST_STEP_VEHICLE_HALTING_NUMBER,
                traci.constants.LAST_STEP_VEHICLE_ID_LIST
            ])
    def get_vehicle_classes(self, vehicle_ids):
        def get_class(vid):
            if res := traci.vehicle.getSubscriptionResults(vid):
                if traci.constants.VAR_VEHICLECLASS in res:
                    return res[traci.constants.VAR_VEHICLECLASS]
            try:
                return traci.vehicle.getVehicleClass(vid)
            except traci.TraCIException:
                return None
        return {vid: get_class(vid) for vid in vehicle_ids}
    # ========================================
    # 3. TRAFFIC LIGHT LOGIC & PHASE MANAGEMENT
    # ========================================
    def _get_traffic_light_logic(self, tl_id):
        try:
            # small TTL to keep in sync with dynamic changes
            ttl = 0.5
            cache = self.tl_logic_cache.get(tl_id)
            now = traci.simulation.getTime()
            current_prog = traci.trafficlight.getProgram(tl_id)
            if cache and (now - cache.get("at", -1e9) < ttl) and cache.get("program") == current_prog:
                return cache["logic"]

            logic = get_current_logic(tl_id)
            if logic:
                self.tl_logic_cache[tl_id] = {"logic": logic, "program": logic.programID, "at": now}
            return logic
        except Exception as e:
            logger.info(f"Error getting active logic for TL {tl_id}: {e}")
            return None
    def _get_phase_count(self, tl_id):
        try:
            logic = self._get_traffic_light_logic(tl_id)
            if logic:
                return len(logic.phases)
            # Proper fallback: use the current program's phase count
            current_def = traci.trafficlight.getCompleteRedYellowGreenDefinition(tl_id)[0]
            return len(current_def.getPhases())
        except Exception as e:
            logger.info(f"Error getting phase count for {tl_id}: {e}")
            return 4
    def _get_phase_name(self, tl_id, phase_idx):
        try:
            logic = self._get_traffic_light_logic(tl_id)
            if logic and phase_idx < len(logic.phases):
                return getattr(logic.phases[phase_idx], 'name', f'phase_{phase_idx}')
        except Exception as e:
            logger.info(f"Error getting phase name for {tl_id}[{phase_idx}]: {e}")
        return f'phase_{phase_idx}'
    def _switch_phase_with_yellow_if_needed(self, tl_id, current_phase, target_phase, logic, controlled_lanes, lane_data, current_time, min_green=None):
        try:
            # Always re-fetch current definition to avoid stale logic mismatch
            try:
                current_def = traci.trafficlight.getCompleteRedYellowGreenDefinition(tl_id)[0]
                n_phases = len(current_def.getPhases())
            except Exception:
                current_def, n_phases = None, 0

            if n_phases == 0:
                return False

            # Clamp indices against current (fresh) logic
            current_phase = max(0, min(int(current_phase), n_phases - 1))
            target_phase = max(0, min(int(target_phase), n_phases - 1))

            # Try to find an existing yellow in the passed-in logic, but verify against current_def before using
            yellow_phase = self._get_yellow_phase(logic or current_def, current_phase, target_phase)
            if yellow_phase is not None:
                # Clamp the yellow index against CURRENT definition before setting
                if 0 <= yellow_phase < n_phases:
                    yellow_duration = self._calculate_adaptive_yellow(tl_id, controlled_lanes, lane_data)
                    if self._safe_set_phase(tl_id, yellow_phase, duration=yellow_duration):
                        self.last_phase_change[tl_id] = current_time
                        self.pending_next_phase[tl_id] = (target_phase, current_time)
                        return True
                    # If setting yellow failed, fallback to APC API
                else:
                    logger.info(f"[YELLOW] Computed yellow idx {yellow_phase} out of range for {tl_id} (0..{n_phases-1})")

            # No usable yellow found: ask APC to synthesize and use it, defers next target via pending_next_phase
            apc = self.adaptive_phase_controllers.get(tl_id, self.apc)
            if apc.insert_yellow_phase_if_needed(current_phase, target_phase):
                self.last_phase_change[tl_id] = current_time
                self.pending_next_phase[tl_id] = (target_phase, current_time)
                return True

            # Last resort: route the switch through APC (it will insert yellow if still needed)
            apc.set_phase_from_API(target_phase, requested_duration=float(min_green or self.adaptive_params['min_green']))
            self.last_phase_change[tl_id] = current_time
            # Invalidate controller cache since logic may mutate inside APC
            self._invalidate_logic_cache(tl_id)
            return False
        except Exception as e:
            logger.info(f"[ERROR] _switch_phase_with_yellow_if_needed({tl_id}) failed: {e}")
            return False
    def _calculate_adaptive_yellow(self, tl_id, controlled_lanes, lane_data):
        try:
            max_speed = 0
            max_queue = 0
            for lane in controlled_lanes:
                queue = lane_data.get(lane, {}).get('queue_length', 0)
                max_queue = max(max_queue, queue)
                for vid in traci.lane.getLastStepVehicleIDs(lane):
                    res = traci.vehicle.getSubscriptionResults(vid)
                    if res and traci.constants.VAR_SPEED in res:
                        s = res[traci.constants.VAR_SPEED]
                        if s > max_speed:
                            max_speed = s
            yellow_time = max(3.0, min(8.0, 1.0 + max_speed / (2 * 3.0) + max_queue / 10.0))  # Extended based on congestion
            return yellow_time
        except Exception as e:
            logger.info(f"Error in _calculate_adaptive_yellow: {e}")
            return 5.0
    def _safe_set_phase(self, tl_id, phase_idx, duration=None):
        try:
            apc = self.adaptive_phase_controllers.get(tl_id)
            if apc:
                safe_idx = apc._safe_phase_index(int(phase_idx), force_reload=True)
                if safe_idx is None:
                    return False
            else:
                logic = get_current_logic(tl_id)
                n = len(logic.getPhases()) if logic else 0
                if n == 0:
                    return False
                safe_idx = max(0, min(int(phase_idx), n - 1))
            traci.trafficlight.setPhase(tl_id, safe_idx)
            if duration is not None:
                traci.trafficlight.setPhaseDuration(tl_id, float(duration))
            return True
        except Exception as e:
            logger.info(f"[ERROR] _safe_set_phase failed for {tl_id}: {e}")
            return False
    def _invalidate_logic_cache(self, tl_id=None):
        try:
            if tl_id is None:
                self.tl_logic_cache.clear()
            else:
                self.tl_logic_cache.pop(tl_id, None)
        except Exception:
            pass
    def _find_phase_for_lane(self, tl_id, target_lane):
        try:
            logic = self._get_traffic_light_logic(tl_id)
            if not logic:
                return 0
            controlled_links = traci.trafficlight.getControlledLinks(tl_id)
            if not controlled_links:
                return 0

            # Gather indices of links originating from target_lane
            link_idxs = [i for i, lk in enumerate(controlled_links) if lk and lk[0] and lk[0][0] == target_lane]
            if not link_idxs:
                return None

            # Pick the first phase that gives green on any of those links
            for pidx, phase in enumerate(logic.phases):
                st = phase.state
                if any((i < len(st) and st[i].upper() == 'G') for i in link_idxs):
                    return pidx

            return None
        except Exception as e:
            logger.info(f"Error finding phase for lane {target_lane} at {tl_id}: {e}")
            return None      
    def _find_best_left_turn_phase(self, tl_id, left_turn_lane, lane_data):
        try:
            logic = self._get_traffic_light_logic(tl_id)
            if not logic: return None
            lanes = traci.trafficlight.getControlledLanes(tl_id)
            idx = lanes.index(left_turn_lane)
            best, score = None, -1
            for i, ph in enumerate(logic.phases):
                state = ph.state.upper()
                if idx >= len(state) or state[idx] != 'G': continue
                s = 20 if all(state[j] in 'rR' or j == idx for j in range(len(state))) else 0
                s -= sum(lane_data.get(l, {}).get('queue_length', 0) * 0.5
                        for j, l in enumerate(lanes) if j != idx and j < len(state) and state[j] == 'G' and lane_data.get(l, {}).get('queue_length', 0) > 5)
                if s > score: best, score = i, s
            return best
        except Exception as e:
            logger.info(f"Error in _find_best_left_turn_phase: {e}")
            return None
    def _add_new_green_phase_for_lane(self, tl_id, lane_id, min_green=None, yellow=3):
        logic = traci.trafficlight.getCompleteRedYellowGreenDefinition(tl_id)[0]
        phases = list(logic.getPhases())
        controlled_lanes = traci.trafficlight.getControlledLanes(tl_id)
        phase_state = ['r'] * len(controlled_lanes)
        try:
            lane_idx = controlled_lanes.index(lane_id)
        except ValueError:
            logger.info(f"[ERROR] Lane {lane_id} not controlled by {tl_id}")
            return None
        min_green = min_green or self.adaptive_params.get('min_green', 10)
        
        # Build green state for the target lane
        phase_state[lane_idx] = 'G'
        green_state_str = "".join(phase_state)
        
        # Retrieve max phases for this TLS (auto-detected from initial logic)
        max_phases = None
        # Preferred: use a controller attribute if set
        if hasattr(self, "tl_max_phases") and tl_id in self.tl_max_phases:
            max_phases = self.tl_max_phases[tl_id]
        else:
            # Fallback: use initial number of phases found in the first loaded logic
            try:
                all_logics = traci.trafficlight.getAllProgramLogics(tl_id)
                if all_logics:
                    max_phases = len(all_logics[0].phases)
                else:
                    max_phases = 12  # fallback default
            except Exception:
                max_phases = 12  # fallback default

        # If we are at or above the phase limit, overwrite instead of appending
        if len(phases) >= max_phases:
            logger.info(f"[PHASE LIMIT] {tl_id} at max phases ({max_phases}), will overwrite")
            apc = self.adaptive_phase_controllers[tl_id]
            idx_to_overwrite = apc.find_phase_to_overwrite(green_state_str)
            if idx_to_overwrite is not None:
                apc.overwrite_phase(idx_to_overwrite, green_state_str, min_green)
                return idx_to_overwrite
            logger.info(f"[PHASE LIMIT] Could not find phase to overwrite, fallback to phase 0")
            return 0

        # Otherwise, append as usual
        # Green phase for the target lane
        new_green_phase = traci.trafficlight.Phase(min_green, green_state_str, 0, 0)
        # Yellow phase for the target lane (only that lane yellow, rest red)
        phase_state[lane_idx] = 'y'
        yellow_state_str = "".join(phase_state)
        new_yellow_phase = traci.trafficlight.Phase(yellow, yellow_state_str, 0, 0)
        # Add both phases to the end
        phases.append(new_green_phase)
        phases.append(new_yellow_phase)
        # Update logic and push to SUMO
        new_logic = traci.trafficlight.Logic(
            logic.programID, logic.type, len(phases) - 2, phases
        )
        traci.trafficlight.setCompleteRedYellowGreenDefinition(tl_id, new_logic)
        self._invalidate_logic_cache()
        logger.info(f"[NEW PHASE] Added green+yellow phase for lane {lane_id} at {tl_id}")

        # Update phase logic cache and RL agent action space if applicable
        if hasattr(self, "tl_logic_cache"):
            self.tl_logic_cache[tl_id] = traci.trafficlight.getAllProgramLogics(tl_id)[0]
        if hasattr(self, "tl_action_sizes"):
            self.tl_action_sizes[tl_id] = len(phases)
        if hasattr(self, "rl_agent") and hasattr(self.rl_agent, "action_size"):
            self.rl_agent.action_size = max(self.tl_action_sizes.values())
        # Optionally encourage RL agent to try new phase
        if hasattr(self, "rl_agent") and hasattr(self.rl_agent, "epsilon"):
            self.rl_agent.epsilon = min(0.7, self.rl_agent.epsilon + 0.4)
        logger.info(f"[ACTION SPACE] tl_id={tl_id} now n_phases={len(phases)}")
        return len(phases) - 2  # return index of new green phase
    def _get_balanced_phase(self, tl_id, lane_data):
        try:
            logic = self._get_traffic_light_logic(tl_id)
            if not logic:
                return 0
            controlled_lanes = traci.trafficlight.getControlledLanes(tl_id)
            best_phase, best_score = 0, -float("inf")
            for phase_idx, phase in enumerate(logic.phases):
                phase_score = 0
                green_lanes = [
                    lane for lane_idx, lane in enumerate(controlled_lanes)
                    if lane_idx < len(phase.state) and phase.state[lane_idx].upper() == 'G'
                ]
                has_vehicle = False
                for lane in green_lanes:
                    if lane in lane_data:
                        q = lane_data[lane]['queue_length']
                        w = lane_data[lane]['waiting_time']
                        if q > 0:
                            has_vehicle = True
                        phase_score += q * 0.8 + w * 0.5

                # Heavy penalty if all green lanes are empty
                if green_lanes and not has_vehicle:
                    phase_score -= 100  # <-- make this large to avoid
                if phase_score > best_score:
                    best_score, best_phase = phase_score, phase_idx
            return best_phase
        except Exception as e:
            logger.info(f"Error in _get_balanced_phase: {e}")
            return 0
    def _safe_phase_index_controller(self, tl_id, idx):
        """
        Clamp the requested phase index to the valid range for the given TLS using ACTIVE logic.
        Returns clamped index or 0 if out of bounds.
        """
        try:
            logic = get_current_logic(tl_id)
            n = len(logic.getPhases()) if logic else 0
            if n <= 0:
                return 0
            if idx < 0 or idx >= n:
                logger.info(f"[BOUNDS] {tl_id}: clamping phase idx {idx} to [0,{n-1}]")
                idx = max(0, min(int(idx), n - 1))
            return idx
        except Exception:
            return 0
    def _get_yellow_phase(self, logic, from_idx, to_idx):
        n_phases = len(logic.phases) if logic else 0
        if not logic or from_idx == to_idx or from_idx >= n_phases or to_idx >= n_phases or from_idx < 0 or to_idx < 0:
            return None
        current = logic.phases[from_idx].state.upper()
        target = logic.phases[to_idx].state.upper()
        n = len(current)
        for phase_idx, phase in enumerate(logic.phases):
            state = phase.state.upper()
            if len(state) != n:
                continue
            matches = True
            for i in range(n):
                if current[i] == 'G' and target[i] == 'R':
                    if state[i] != 'Y':
                        matches = False
                        break
                elif state[i] != current[i]:
                    matches = False
                    break
            if matches:
                return phase_idx
        return None
    def _phase_has_traffic(self, logic, action, controlled_lanes, lane_data):
        phase_state = logic.phases[action].state
        for lane_idx, lane in enumerate(controlled_lanes):
            if lane_idx < len(phase_state) and phase_state[lane_idx].upper() == 'G':
                if lane_data.get(lane, {}).get("queue_length", 0) > 0:
                    return True
        return False
    def safe_phase_transition_check(self, tl_id, current_phase, target_phase):
        """Ensure safe transition with proper yellow phase"""
        try:
            # Check if vehicles are approaching at high speed
            controlled_lanes = traci.trafficlight.getControlledLanes(tl_id)
            logic = self._get_traffic_light_logic(tl_id)
            
            if not logic or current_phase >= len(logic.phases):
                return True
                
            current_state = logic.phases[current_phase].state
            
            for i, lane in enumerate(controlled_lanes):
                if i >= len(current_state):
                    continue
                    
                # If this lane is currently green
                if current_state[i].upper() == 'G':
                    # Check for approaching vehicles
                    vehicles = traci.lane.getLastStepVehicleIDs(lane)
                    for vid in vehicles:
                        try:
                            speed = traci.vehicle.getSpeed(vid)
                            distance = traci.lane.getLength(lane) - traci.vehicle.getLanePosition(vid)
                            
                            # If vehicle is approaching fast and close
                            if speed > 8 and distance < speed * 3:  # 3 second rule
                                # Need yellow phase
                                return False
                        except:
                            continue
            
            return True
        except Exception as e:
            self.logger.error(f"Phase transition safety check failed: {e}")
            return True
    # ========================================
    # 4. PRIORITY & EMERGENCY HANDLING
    # ========================================    
    def _handle_priority_conditions(self, tl_id, controlled_lanes, lane_data, current_time):
        amb = [l for l in controlled_lanes if lane_data.get(l, {}).get('ambulance')]
        if amb: return self._handle_ambulance_priority(tl_id, amb, lane_data, current_time)
        left = [l for l in controlled_lanes if lane_data.get(l, {}).get('left_turn') and
                (lane_data[l]['queue_length'] > 3 or lane_data[l]['waiting_time'] > 10)]
        if left: return self._handle_protected_left_turn(tl_id, left, lane_data, current_time)
        return False
    def _handle_ambulance_priority(self, tl_id, controlled_lanes, lane_data, current_time):
        try:
            amb_lanes = [l for l in controlled_lanes if lane_data.get(l, {}).get('ambulance')]
            if not amb_lanes: return False
            min_dist, target = float('inf'), None
            for lane in amb_lanes:
                try:
                    for vid in traci.lane.getLastStepVehicleIDs(lane):
                        if traci.vehicle.getVehicleClass(vid) in ['emergency', 'authority']:
                            d = traci.lane.getLength(lane) - traci.vehicle.getLanePosition(vid)
                            if d < min_dist: min_dist, target = d, lane
                except Exception as e:
                    logger.info(f"Error ambulance lane {lane}: {e}")
            if target is None: return False
            phase = self._find_phase_for_lane(tl_id, target)
            if phase is not None:
                dur = 30 if min_dist < 30 else 20
                traci.trafficlight.setPhase(tl_id, phase)
                traci.trafficlight.setPhaseDuration(tl_id, dur)
                self.ambulance_active[tl_id] = True
                self.ambulance_start_time[tl_id] = current_time
                self.rl_agent.training_data.append({'event':'ambulance_priority','lane_id':target,'tl_id':tl_id,
                    'phase':phase,'simulation_time':current_time,'distance_to_stopline':min_dist,'duration':dur})
                return True
            return False
        except Exception as e:
            logger.info(f"Error in _handle_ambulance_priority: {e}")
            return False
    def _handle_protected_left_turn(self, tl_id, controlled_lanes, lane_data, current_time):
        try:
            apc = self.adaptive_phase_controllers.get(tl_id, self.apc)
            # Find left-turn lanes needing service (blocked: queue > 0, all vehicles stopped)
            left_candidates = []
            for lane in controlled_lanes:
                links = traci.lane.getLinks(lane)
                is_left = any(len(link) > 6 and link[6] == 'l' for link in links)
                if not is_left:
                    continue
                vehicles = traci.lane.getLastStepVehicleIDs(lane)
                if not vehicles:
                    continue
                speeds = [traci.vehicle.getSpeed(v) for v in vehicles]
                if speeds and max(speeds) < 0.2 and lane_data.get(lane, {}).get('queue_length', 0) > 0:
                    left_candidates.append((lane, len(vehicles)))

            if not left_candidates:
                return False

            # Most blocked left
            target, _ = max(left_candidates, key=lambda x: x[1])

            # Create/find protected-left phase via APC (link-based and cache-safe)
            phase_idx = apc.create_protected_left_phase_for_lane(target)
            if phase_idx is None:
                return False

            # Duration from Supabase record if present, else APC max_green
            phase_record = apc.load_phase_from_supabase(phase_idx)
            dur = float(phase_record["duration"]) if phase_record else float(apc.max_green)

            success = apc.set_phase_from_API(phase_idx, requested_duration=dur)
            if success:
                self.last_phase_change[tl_id] = current_time
                self.phase_utilization[(tl_id, phase_idx)] = self.phase_utilization.get((tl_id, phase_idx), 0) + 1
                logger.info(f"[PROTECTED LEFT] Served at {tl_id} for lane {target} (phase {phase_idx})")
                # PATCH: Use correct per-TLS RL agent
                rl_agent = self.rl_agents[tl_id] if hasattr(self, 'rl_agents') and tl_id in self.rl_agents else self.rl_agent
                rl_agent.training_data.append({
                    'event': 'protected_left_served',
                    'lane_id': target,
                    'tl_id': tl_id,
                    'phase': phase_idx,
                    'simulation_time': current_time
                })
                return True
            else:
                logger.info(f"[PROTECTED LEFT][FAIL] Could not set phase {phase_idx} at {tl_id} for lane {target}")
                return False
        except Exception as e:
            logger.info(f"Error in _handle_protected_left_turn: {e}")
            return False    
    def _detect_priority_vehicles(self, lane_id):
        try:
            return any(traci.vehicle.getVehicleClass(vid) in ['emergency', 'authority']
                    for vid in traci.lane.getLastStepVehicleIDs(lane_id))
        except: return False
    def block_conflicting_phases(self, tls_id, priority_lane, duration=30):
        """Temporarily block phases that conflict with priority lane"""
        try:
            apc = self.adaptive_phase_controllers[tls_id]
            priority_phase = apc.find_phase_for_lane(priority_lane)
            
            if priority_phase is not None:
                # Find conflicting phases
                logic = traci.trafficlight.getAllProgramLogics(tls_id)[0]
                controlled_lanes = traci.trafficlight.getControlledLanes(tls_id)
                
                priority_state = logic.phases[priority_phase].state
                
                for phase_idx, phase in enumerate(logic.phases):
                    if phase_idx == priority_phase:
                        continue
                        
                    # Check for conflicts
                    has_conflict = False
                    for i in range(min(len(phase.state), len(priority_state))):
                        if phase.state[i].upper() == 'G' and priority_state[i].upper() == 'G':
                            # Check if movements conflict (simplified)
                            has_conflict = True
                            break
                    
                    if has_conflict:
                        # Temporarily reduce this phase
                        apc.phase_duration_multiplier[phase_idx] = 0.3
                        
                # Schedule restoration
                self._schedule_phase_restoration(tls_id, duration)
        except Exception as e:
            self.logger.error(f"Error blocking conflicting phases: {e}")
    def _schedule_phase_restoration(self, tls_id, duration):
        """Schedule restoration of normal phase durations"""
        # You could use threading.Timer or track this in run_step
        # For simplicity, just track the time
        restoration_time = traci.simulation.getTime() + duration
        if not hasattr(self, 'scheduled_restorations'):
            self.scheduled_restorations = {}
        self.scheduled_restorations[tls_id] = restoration_time
    # ========================================
    # 5. CONGESTION & SPILLBACK MANAGEMENT
    # ========================================    
    def prevent_spillback(self, tls_id):
        """Prevent queue spillback to upstream intersections"""
        try:
            apc = self.adaptive_phase_controllers[tls_id]
            
            for lane_id in apc.lane_ids:
                queue_length = traci.lane.getLastStepHaltingNumber(lane_id)
                lane_length = traci.lane.getLength(lane_id)
                
                queue_ratio = queue_length * 7.5 / max(lane_length, 1)
                
                if queue_ratio > 0.7:
                    upstream_tls = self.get_upstream_tls(lane_id)
                    
                    if upstream_tls and upstream_tls in self.adaptive_phase_controllers:
                        upstream_apc = self.adaptive_phase_controllers[upstream_tls]
                        
                        feeding_phases = self.get_feeding_phases(upstream_tls, lane_id)
                        for phase_idx in feeding_phases:
                            upstream_apc.phase_duration_multiplier[phase_idx] = 0.7
                            
                        self.logger.info(f"[SPILLBACK PREVENTION] Reducing flow from {upstream_tls} to {tls_id}")
        except Exception as e:
            self.logger.error(f"Error preventing spillback: {e}")
    def get_upstream_tls(self, lane_id):
        """Find upstream traffic light"""
        try:
            edge = traci.lane.getEdgeID(lane_id)
            # Get incoming edges
            for tls_id in self.adaptive_phase_controllers:
                controlled_lanes = traci.trafficlight.getControlledLanes(tls_id)
                for lane in controlled_lanes:
                    links = traci.lane.getLinks(lane)
                    for link in links:
                        if link[0] and traci.lane.getEdgeID(link[0]) == edge:
                            return tls_id
            return None
        except Exception:
            return None
    def get_feeding_phases(self, tls_id, target_lane_id):
        """Get phases that feed into target lane"""
        feeding_phases = []
        try:
            target_edge = traci.lane.getEdgeID(target_lane_id)
            controlled_lanes = traci.trafficlight.getControlledLanes(tls_id)
            logic = traci.trafficlight.getAllProgramLogics(tls_id)[0]
            
            for phase_idx, phase in enumerate(logic.phases):
                for i, lane in enumerate(controlled_lanes):
                    if i < len(phase.state) and phase.state[i].upper() == 'G':
                        links = traci.lane.getLinks(lane)
                        for link in links:
                            if link[0] and traci.lane.getEdgeID(link[0]) == target_edge:
                                feeding_phases.append(phase_idx)
                                break
        except Exception:
            pass
        return feeding_phases
    def request_upstream_metering(self, tls_id, congested_lane):
        """Request upstream intersections to meter flow"""
        try:
            upstream_tls = self.get_upstream_tls(congested_lane)
            if upstream_tls and upstream_tls in self.adaptive_phase_controllers:
                upstream_apc = self.adaptive_phase_controllers[upstream_tls]
                
                # Implement metering logic
                upstream_apc.metering_active = True
                upstream_apc.metering_target = congested_lane
                
                self.logger.info(f"[METERING] Requested from {upstream_tls} for {congested_lane}")
        except Exception as e:
            self.logger.error(f"Error requesting metering: {e}")
    def monitor_congestion_status(self):
        """Monitor and report congestion status across all intersections"""
        if not self.corridor:
            return {'clusters': 0, 'active_responses': 0, 'intersections': {}}
            
        congestion_report = {
            'timestamp': traci.simulation.getTime(),
            'clusters': len(self.corridor._congestion_clusters),
            'active_responses': len(self.corridor._active_responses),
            'intersections': {}
        }
        
        for tl_id in self.adaptive_phase_controllers:
            severity = self.corridor._calculate_tl_congestion_severity(tl_id)
            response = self.corridor._active_responses.get(tl_id, "none")
            effectiveness = self.corridor._response_effectiveness.get(tl_id, 0)
            
            congestion_report['intersections'][tl_id] = {
                'severity': severity,
                'response': response,
                'effectiveness': effectiveness
            }
            
            # Log critical congestion
            if severity > 0.8:
                logger.warning(f"[CRITICAL] {tl_id} congestion severity: {severity:.2f}")
        
        return congestion_report
    def activate_global_congestion_mode(self):
        """Activate congestion mode for all intersections"""
        if self.congestion_mode_active:
            return
            
        self.logger.info("[GLOBAL CONGESTION MODE] Activated")
        self.congestion_mode_active = True
        
        for tls_id in self.adaptive_phase_controllers:
            apc = self.adaptive_phase_controllers[tls_id]
            apc.activate_congestion_mode()
    def activate_emergency_congestion_mode(self):
        """Activate emergency mode for severe network congestion"""
        logger.warning("[EMERGENCY MODE] Activating emergency congestion protocols")
        
        for tl_id, apc in self.adaptive_phase_controllers.items():
            # Force aggressive settings
            apc.min_green = 5
            apc.max_green = 120
            apc.severe_congestion_threshold = 5
            
            # Find and prioritize highest queue lane
            max_queue = 0
            max_queue_lane = None
            for lane in apc.lane_ids:
                queue = traci.lane.getLastStepHaltingNumber(lane)
                if queue > max_queue:
                    max_queue = queue
                    max_queue_lane = lane
                    
            if max_queue > 30 and max_queue_lane:
                phase = apc.find_or_create_phase_for_lane(max_queue_lane)
                if phase is not None:
                    apc.set_phase_from_API(phase, requested_duration=90)
                    logger.info(f"[EMERGENCY] {tl_id}: Serving {max_queue_lane} with {max_queue} vehicles")
    # ========================================
    # 6. ARTERIAL & CORRIDOR COORDINATION
    # ========================================    
    def coordinate_arterial_flow(self):
        """Coordinate multiple intersections for arterial flow"""
        try:
            arterial_lanes = self.identify_arterial_lanes()
            
            for tls_id in self.adaptive_phase_controllers:
                apc = self.adaptive_phase_controllers[tls_id]
                
                distance_from_start = self.get_arterial_distance(tls_id)
                travel_time = distance_from_start / 13.89
                
                offset = travel_time % self.cycle_time
                apc.phase_offset = offset
                
                arterial_phases = self.get_arterial_phases(tls_id)
                for phase_idx in arterial_phases:
                    apc.phase_weights[phase_idx] = 1.5
        except Exception as e:
            self.logger.error(f"Error coordinating arterial flow: {e}")
    def identify_arterial_lanes(self):
        """Identify main arterial based on traffic volume"""
        try:
            lane_volumes = {}
            for lane_id in self.lane_id_list:
                volume = traci.lane.getLastStepVehicleNumber(lane_id)
                edge_id = traci.lane.getEdgeID(lane_id)
                lane_volumes[edge_id] = lane_volumes.get(edge_id, 0) + volume
            
            sorted_edges = sorted(lane_volumes.items(), key=lambda x: x[1], reverse=True)
            arterial_edges = [edge for edge, volume in sorted_edges[:3]]
            
            self.arterial_lanes = [lane for lane in self.lane_id_list 
                                  if traci.lane.getEdgeID(lane) in arterial_edges]
            return self.arterial_lanes
        except Exception as e:
            self.logger.error(f"Error identifying arterial lanes: {e}")
            return []
    def get_arterial_distance(self, tls_id):
        """Get distance of TLS from arterial start"""
        # This is a simplified version - you'll need to implement based on your network
        try:
            # You could use junction positions
            pos = traci.junction.getPosition(tls_id)
            # Calculate distance from first junction
            return abs(pos[0]) + abs(pos[1])  # Manhattan distance as example
        except Exception:
            return 0
    def get_arterial_phases(self, tls_id):
        """Get phases that serve arterial lanes"""
        try:
            arterial_phases = []
            apc = self.adaptive_phase_controllers[tls_id]
            
            for phase_idx in range(len(traci.trafficlight.getAllProgramLogics(tls_id)[0].phases)):
                # Check if this phase serves arterial lanes
                phase_state = traci.trafficlight.getAllProgramLogics(tls_id)[0].phases[phase_idx].state
                controlled_lanes = traci.trafficlight.getControlledLanes(tls_id)
                
                for i, lane in enumerate(controlled_lanes):
                    if i < len(phase_state) and phase_state[i].upper() == 'G':
                        if lane in self.arterial_lanes:
                            arterial_phases.append(phase_idx)
                            break
            
            return arterial_phases
        except Exception:
            return []
    # ========================================
    # 7. LANE ANALYSIS & METRICS
    # ========================================    
    def _collect_enhanced_lane_data(self, vehicle_classes, all_vehicles):
        lane_data = {}
        lane_vehicle_ids = {}
        lane_ids = self.lane_id_list

        # Batch subscription results for all lanes
        results_dict = {lid: traci.lane.getSubscriptionResults(lid) for lid in lane_ids}

        # Preallocate numpy arrays for bulk stats
        vehicle_count = np.zeros(len(lane_ids), dtype=np.float32)
        mean_speed = np.zeros(len(lane_ids), dtype=np.float32)
        queue_length = np.zeros(len(lane_ids), dtype=np.float32)
        waiting_time = np.zeros(len(lane_ids), dtype=np.float32)
        lane_length = np.array([self.lane_lengths.get(lid, 1.0) for lid in lane_ids], dtype=np.float32)
        ambulance_mask = np.zeros(len(lane_ids), dtype=bool)

        # Prepare vehicle ids array for all lanes
        vehicle_ids_arr = []

        # Gather all lane stats at once, vectorized where possible
        for idx, lane_id in enumerate(lane_ids):
            results = results_dict[lane_id]
            if not results:
                # Even if no subscription results, we can still compute waiting time
                try:
                    waiting_time[idx] = float(traci.lane.getWaitingTime(lane_id))
                except Exception:
                    waiting_time[idx] = 0.0
                vehicle_ids_arr.append([])
                continue

            vids = results.get(traci.constants.LAST_STEP_VEHICLE_ID_LIST, [])
            vids = [vid for vid in vids if vid in all_vehicles]
            vehicle_ids_arr.append(vids)

            vehicle_count[idx] = results.get(traci.constants.LAST_STEP_VEHICLE_NUMBER, 0)
            mean_speed[idx] = max(results.get(traci.constants.LAST_STEP_MEAN_SPEED, 0), 0.0)
            queue_length[idx] = results.get(traci.constants.LAST_STEP_VEHICLE_HALTING_NUMBER, 0)

            # PATCH: waiting time must be queried via API (no lane subscription constant exists)
            try:
                waiting_time[idx] = float(traci.lane.getWaitingTime(lane_id))
            except Exception:
                waiting_time[idx] = 0.0

            ambulance_mask[idx] = any(vehicle_classes.get(vid) == 'emergency' for vid in vids)

        densities = np.divide(vehicle_count, lane_length, out=np.zeros_like(vehicle_count), where=lane_length > 0)
        left_turn_mask = np.array([lid in self.left_turn_lanes for lid in lane_ids])
        right_turn_mask = np.array([lid in self.right_turn_lanes for lid in lane_ids])

        # Batch fill lane_data dicts
        for idx, lane_id in enumerate(lane_ids):
            vids = vehicle_ids_arr[idx]

            def safe_count_classes(vids_inner):
                counts = defaultdict(int)
                for vid in vids_inner:
                    vclass = vehicle_classes.get(vid)
                    if vclass:
                        counts[vclass] += 1
                return counts

            lane_data[lane_id] = {
                'queue_length': float(queue_length[idx]),
                'waiting_time': float(waiting_time[idx]),
                'density': float(densities[idx]),
                'mean_speed': float(mean_speed[idx]),
                'vehicle_ids': vids,
                'flow': float(vehicle_count[idx]),
                'lane_id': lane_id,
                'edge_id': self.lane_edge_ids.get(lane_id, ""),
                'route_id': self._get_route_for_lane(lane_id, all_vehicles),
                'ambulance': bool(ambulance_mask[idx]),
                'vehicle_classes': safe_count_classes(vids),
                'left_turn': bool(left_turn_mask[idx]),
                'right_turn': bool(right_turn_mask[idx]),
                'tl_id': self.lane_to_tl.get(lane_id, '')
            }

        self.lane_vehicle_ids = lane_vehicle_ids  # Store for later if needed
        return lane_data
    def _get_route_for_lane(self, lane_id, all_vehicles):
        try:
            vehicles = [vid for vid in self.lane_vehicle_ids.get(lane_id, []) if vid in all_vehicles]
            return traci.vehicle.getRouteID(vehicles[0]) if vehicles else ""
        except:
            return ""
    def _update_lane_status_and_score(self, lane_data):
        status = {}
        try:
            for lane_id, data in lane_data.items():
                idx = self.lane_id_to_idx[lane_id]
                norm = lambda x: data[x] / self.norm_bounds[x]
                queue_norm, wait_norm = norm('queue_length'), norm('waiting_time')
                speed_norm, flow_norm = norm('mean_speed'), norm('flow')
                arrival_norm = self._calculate_arrival_rate(lane_id) / self.norm_bounds['arrival_rate']
                composite = (self.adaptive_params['queue_weight'] * queue_norm +
                            self.adaptive_params['wait_weight'] * wait_norm +
                            1 - min(speed_norm, 1.0) + 1 - min(flow_norm, 1.0) +
                            arrival_norm * 0.5)
                if composite > 0.8:
                    stat, delta = "BAD", -max(2, min(8, int(composite * 8)))
                elif composite < 0.3:
                    stat, delta = "GOOD", max(2, min(8, int((1 - composite) * 8)))
                else:
                    stat, delta = "NORMAL", 0
                if self.lane_states[idx] == stat:
                    self.consecutive_states[idx] += 1
                    delta *= min(3.0, 1.0 + self.consecutive_states[idx] * 0.1)
                else:
                    self.lane_states[idx] = stat
                    self.consecutive_states[idx] = 1
                self.lane_scores[idx] += delta
                if stat == "NORMAL":
                    decay = 1.5 if composite < 0.4 else 1.0
                    self.lane_scores[idx] = (max(0, self.lane_scores[idx] - decay) if self.lane_scores[idx] > 0
                                            else min(0, self.lane_scores[idx] + decay) if self.lane_scores[idx] < 0
                                            else 0)
                self.lane_scores[idx] = max(-50, min(50, self.lane_scores[idx]))
                status[lane_id] = stat
        except Exception as e:
            logger.info(f"Error in _update_lane_status_and_score: {e}")
        return status
    def _calculate_arrival_rate(self, lane_id):
        try:
            idx = self.lane_id_to_idx[lane_id]
            now = traci.simulation.getTime()
            lane_last_green = self.last_green_time.get(lane_id, 0)
            # Use adaptive_params' min_green instead of a missing attribute
            min_green = self.adaptive_params.get('min_green', 10)
            if now - lane_last_green < min_green:
                # This lane was just served; skip special event for now
                return 0.0
            # Else, compute arrivals since last time
            curr = set(traci.lane.getLastStepVehicleIDs(lane_id))
            arrivals = curr - self.last_lane_vehicles[idx]
            delta_time = max(1e-3, now - self.last_arrival_time[idx])
            rate = len(arrivals) / delta_time
            self.last_lane_vehicles[idx], self.last_arrival_time[idx] = curr, now
            return rate
        except Exception as e:
            logger.info(f"Error calculating arrival rate for {lane_id}: {e}")
            return 0.0
    def _select_target_lane(self, tl_id, controlled_lanes, lane_data, current_time):
        nonempty_lanes = [l for l in controlled_lanes if l in lane_data and lane_data[l]['queue_length'] > 0]
        lanes_to_consider = nonempty_lanes if nonempty_lanes else [l for l in controlled_lanes if l in lane_data]
        if not lanes_to_consider:
            return None

        max_queue = max((lane_data[l]['queue_length'] for l in lanes_to_consider), default=1)
        max_wait = max((lane_data[l]['waiting_time'] for l in lanes_to_consider), default=1)
        max_arr = max((lane_data[l].get('arrival_rate', 0) for l in lanes_to_consider), default=0.1)

        candidates = []
        for lane in lanes_to_consider:
            d = lane_data[lane]
            idx = self.lane_id_to_idx[lane]
            q_score = d['queue_length'] / max_queue if max_queue > 0 else 0
            w_score = d['waiting_time'] / max_wait if max_wait > 0 else 0
            a_score = d.get('arrival_rate', 0) / max_arr if max_arr > 0 else 0
            last_green = self.last_green_time[idx]
            starve = max(0, (current_time - last_green - self.adaptive_params['starvation_threshold']) / 10)
            emerg = 2 if d.get('ambulance') else 0
            total = (0.5 * q_score + 0.3 * w_score + 0.2 * a_score + 0.3 * starve + emerg)
            candidates.append((lane, total))
        # Pick the lane with the highest score
        return max(candidates, key=lambda x: x[1])[0]
    def _is_in_dilemma_zone(self, tl_id, controlled_lanes, lane_data):
        try:
            logic = self._get_traffic_light_logic(tl_id)
            if not logic:
                return False
            current_phase_index = traci.trafficlight.getPhase(tl_id)
            phases = getattr(logic, "phases", None)
            if phases is None or current_phase_index >= len(phases) or current_phase_index < 0:
                logger.info(f"Error in _is_in_dilemma_zone: current_phase_index {current_phase_index} out of range for {tl_id} (phases: {len(phases) if phases else 'N/A'})")
                return False
            state = phases[current_phase_index].state
            n = min(len(state), len(controlled_lanes))
            for lane_idx in range(n):
                lane = controlled_lanes[lane_idx]
                if state[lane_idx].upper() == 'G':
                    for vid in traci.lane.getLastStepVehicleIDs(lane):
                        dist = traci.lane.getLength(lane) - traci.vehicle.getLanePosition(vid)
                        if 0 < dist <= self.DILEMMA_ZONE_THRESHOLD:
                            return True
            return False
        except Exception as e:
            logger.info(f"Error in _is_in_dilemma_zone: {e}")
            return False
    def _find_starved_lane(self, controlled_lanes, current_time):
        for lane in controlled_lanes:
            idx = self.lane_id_to_idx.get(lane)
            if idx is not None and current_time - self.last_green_time[idx] > self.adaptive_params['starvation_threshold']:
                return lane
        return None
    def _is_left_turn_lane(self, lane_id):
        return lane_id in self.left_turn_lanes
    def _lanes_conflict(self, lane1, lane2):
        """Check if two lanes have conflicting movements"""
        try:
            # Get lane connections
            links1 = traci.lane.getLinks(lane1)
            links2 = traci.lane.getLinks(lane2)
            
            if not links1 or not links2:
                return True  # Assume conflict if no info
            
            # Check if target lanes intersect
            targets1 = {link[0] for link in links1 if link}
            targets2 = {link[0] for link in links2 if link}
            
            # If they go to the same target lane, they conflict
            if targets1 & targets2:
                return True
            
            # Check if paths cross (simplified)
            # This is a simplified check - you may need more sophisticated geometry
            edge1 = traci.lane.getEdgeID(lane1)
            edge2 = traci.lane.getEdgeID(lane2)
            
            # Same edge = likely conflict (e.g., left turn vs straight)
            if edge1 == edge2:
                return True
            
            # Different edges - check if opposite (simplified)
            # You may want to add actual geometric checks here
            return False
            
        except Exception:
            return True  # Assume conflict on error
    def _calculate_dynamic_green(self, lane_data):
        base = self.adaptive_params['min_green']
        queue = min(lane_data['queue_length'] * 0.7, 15)
        density = min(lane_data['density'] * 5, 10)
        bonus = 10 if lane_data.get('ambulance') else 0
        total = base + queue + density + bonus
        return min(max(total, base), self.adaptive_params['max_green'])
    # ========================================
    # 8. PERFORMANCE MONITORING
    # ========================================    
    def log_phase_event(self, event: dict):
        event["timestamp"] = datetime.datetime.now().isoformat()
        self.phase_events.append(event)
        # Flush at most every 5 seconds
        if time.time() - self._last_phase_event_flush >= 5.0:
            try:
                with open(self.phase_event_log_file, "wb") as f:
                    pickle.dump(self.phase_events[-5000:], f, protocol=pickle.HIGHEST_PROTOCOL)
            except Exception as e:
                logger.info(f"[WARN] Could not write phase_events to file: {e}")
            self._last_phase_event_flush = time.time()
    def monitor_performance_metrics(self):
        """Track key performance indicators"""
        try:
            metrics = {
                'average_delay': 0,
                'total_waiting_time': 0,
                'average_queue_length': 0,
                'throughput': 0,
                'congestion_events': 0
            }
            
            for tls_id in self.adaptive_phase_controllers:
                apc = self.adaptive_phase_controllers[tls_id]
                
                for lane in apc.lane_ids:
                    metrics['total_waiting_time'] += traci.lane.getWaitingTime(lane)
                    metrics['average_queue_length'] += traci.lane.getLastStepHaltingNumber(lane)
                    metrics['throughput'] += traci.lane.getLastStepVehicleNumber(lane)
                    
                    if apc.calculate_congestion_severity(lane) > 0.7:
                        metrics['congestion_events'] += 1
            
            total_lanes = sum(len(apc.lane_ids) for apc in self.adaptive_phase_controllers.values())
            if total_lanes > 0:
                metrics['average_queue_length'] /= total_lanes
                metrics['average_delay'] = metrics['total_waiting_time'] / max(1, metrics['throughput'])
            
            if metrics['congestion_events'] > total_lanes * 0.3:
                self.activate_global_congestion_mode()
            
            return metrics
        except Exception as e:
            self.logger.error(f"Error monitoring metrics: {e}")
            return {}
    def _get_phase_efficiency(self, tl_id, phase_index):
        try:
            total = sum(c for (tl, _), c in self.phase_utilization.items() if tl == tl_id)
            if not total: return 1.0
            count = self.phase_utilization.get((tl_id, phase_index), 0)
            return min(1.0, max(0.1, count/total))
        except: return 1.0
    def debug_green_lanes(self, tl_id, lane_data):
        logic = get_current_logic(tl_id)
        current_phase = traci.trafficlight.getPhase(tl_id)
        if not logic:
            return
        phases = logic.getPhases()
        if not (0 <= current_phase < len(phases)):
            logger.info(f"[ERROR] Current phase {current_phase} is out of range for {tl_id} (phases: {len(phases)})")
            return
        phase_state = phases[current_phase].state
        controlled_lanes = traci.trafficlight.getControlledLanes(tl_id)
        for lane_idx, lane in enumerate(controlled_lanes):
            if lane_idx < len(phase_state) and phase_state[lane_idx].upper() == 'G':
                _ = lane_data.get(lane, {}).get("queue_length", None)
                # Add more debug if needed              
    # ========================================
    # 9. CONTROL EXECUTION
    # ========================================
    def run_step(self):
        """Main control step with proper corridor coordination"""
        try:
            self.step_count += 1
            current_time = traci.simulation.getTime()
            self.intersection_data = {}
            
            # Step 1: Corridor coordination (highest priority)
            if self.corridor is not None and self.step_count % 100 == 0:
                try:
                    # Update corridor state and detect congestion
                    self.corridor.step(current_time=current_time)
                    
                    # Log active congestion management
                    if self.corridor._congestion_clusters:
                        total_tls_in_clusters = sum(len(cluster) for cluster in self.corridor._congestion_clusters)
                        logger.info(f"[CONGESTION] {len(self.corridor._congestion_clusters)} active clusters "
                                f"affecting {total_tls_in_clusters} intersections")
                        
                        # Log specific responses
                        for tl_id, response_type in self.corridor._active_responses.items():
                            logger.debug(f"[CORRIDOR] {tl_id}: {response_type} response active")
                            
                except Exception as e:
                    logger.error(f"[CORRIDOR] Error in coordinator step: {e}")
                    import traceback
                    traceback.print_exc()
            
            # Step 2: Individual APC control (respects corridor constraints)
            for tl_id, apc in self.adaptive_phase_controllers.items():
                try:
                    # Check if under corridor control
                    if self.corridor and tl_id in self.corridor._active_responses:
                        response_type = self.corridor._active_responses[tl_id]
                        logger.debug(f"[CONTROL] {tl_id} under {response_type} coordination")
                    
                    # Run APC control step (will respect coordinator masks/constraints)
                    apc.control_step()
                    
                except Exception as e:
                    logger.error(f"[APC] Error in control_step for {tl_id}: {e}")
                    continue
            
            # Step 3: Defensive re-initialization for dynamic networks
            for tl_id in traci.trafficlight.getIDList():
                if tl_id not in self.adaptive_phase_controllers:
                    lanes = traci.trafficlight.getControlledLanes(tl_id)
                    logger.info(f"[DYNAMIC] Adding new traffic light: {tl_id}")
                    
                    apc = AdaptivePhaseController(
                        lane_ids=lanes,
                        tls_id=tl_id,
                        alpha=1.0,
                        min_green=10,
                        max_green=60
                    )
                    apc.controller = self
                    self.adaptive_phase_controllers[tl_id] = apc
                    
                    n_phases = len(traci.trafficlight.getAllProgramLogics(tl_id)[0].phases)
                    rl_agent = EnhancedQLearningAgent(
                        state_size=12,
                        action_size=n_phases,
                        adaptive_controller=apc,
                        mode=self.mode,
                        coordinator=self.corridor  # Pass coordinator to new agents
                    )
                    self.rl_agents[tl_id] = rl_agent
                    apc.rl_agent = rl_agent
            
            # Step 4: Collect network-wide data
            all_vehicles = set(traci.vehicle.getIDList())
            vehicle_classes = self.get_vehicle_classes(all_vehicles)
            lane_data = self._collect_enhanced_lane_data(vehicle_classes, all_vehicles)
            
            # Subscribe to new vehicles
            new_vehicles = all_vehicles - self.subscribed_vehicles
            if new_vehicles:
                self.subscribe_vehicles(new_vehicles)
                self.subscribed_vehicles.update(new_vehicles)
            
            # Step 5: Process each intersection (legacy compatibility)
            for tl_id in traci.trafficlight.getIDList():
                try:
                    apc = self.adaptive_phase_controllers[tl_id]
                    rl_agent = self.rl_agents[tl_id]
                    controlled_lanes = traci.trafficlight.getControlledLanes(tl_id)
                    logic = self._get_traffic_light_logic(tl_id)
                    current_phase = traci.trafficlight.getPhase(tl_id)
                    
                    # Handle pending phase transitions
                    if tl_id in self.pending_next_phase:
                        pending_phase, set_time = self.pending_next_phase[tl_id]
                        n_phases = len(logic.phases) if logic else 0
                        
                        if logic and 0 <= current_phase < n_phases:
                            phase_duration = logic.phases[current_phase].duration
                        else:
                            phase_duration = 3
                            
                        pending_phase = self._safe_phase_index_controller(tl_id, pending_phase)
                        if n_phases == 0 or pending_phase >= n_phases or pending_phase < 0:
                            logger.warning(f"[WARNING] Pending phase {pending_phase} for {tl_id} out of bounds")
                            pending_phase = 0
                            
                        if current_time - set_time >= phase_duration - 0.1:
                            apc.set_phase_from_API(pending_phase)
                            self.last_phase_change[tl_id] = current_time
                            del self.pending_next_phase[tl_id]
                            logic = self._get_traffic_light_logic(tl_id)
                            n_phases = len(logic.phases) if logic else 0
                            current_phase = traci.trafficlight.getPhase(tl_id)
                            if n_phases == 0 or current_phase >= n_phases or current_phase < 0:
                                apc.set_phase_from_API(max(0, n_phases - 1))
                        continue
                    
                    # Skip if under active corridor emergency response
                    if (self.corridor and 
                        tl_id in self.corridor._active_responses and
                        self.corridor._active_responses[tl_id] in ["bottleneck", "metering"]):
                        # Corridor is handling this intersection
                        continue
                    
                    # Priority handling (ambulance, protected left)
                    if self._handle_ambulance_priority(tl_id, controlled_lanes, lane_data, current_time):
                        continue
                    if self._handle_protected_left_turn(tl_id, controlled_lanes, lane_data, current_time):
                        continue
                    
                    # Starvation detection
                    starved_lanes = []
                    for lane in controlled_lanes:
                        idx = self.lane_id_to_idx.get(lane)
                        if idx is not None and lane in lane_data and lane_data[lane]['queue_length'] > 0:
                            time_since_green = current_time - self.last_green_time[idx]
                            if time_since_green > self.adaptive_params['starvation_threshold']:
                                starved_lanes.append((lane, time_since_green))
                                
                    if starved_lanes:
                        most_starved_lane = max(starved_lanes, key=lambda x: x[1])[0]
                        starved_phase = self._find_phase_for_lane(tl_id, most_starved_lane)
                        new_phase_added = False
                        
                        if starved_phase is None:
                            starved_phase = self._add_new_green_phase_for_lane(
                                tl_id, most_starved_lane, 
                                min_green=self.adaptive_params['min_green'], 
                                yellow=3
                            )
                            logic = self._get_traffic_light_logic(tl_id)
                            new_phase_added = True
                            
                        if starved_phase is not None and current_phase != starved_phase:
                            switched = self._switch_phase_with_yellow_if_needed(
                                tl_id, current_phase, starved_phase, logic, 
                                controlled_lanes, lane_data, current_time
                            )
                            logic = self._get_traffic_light_logic(tl_id)
                            n_phases = len(logic.phases) if logic else 1
                            current_phase = traci.trafficlight.getPhase(tl_id)
                            
                            if current_phase >= n_phases:
                                apc.set_phase_from_API(n_phases - 1)
                            if not switched:
                                apc.set_phase_from_API(starved_phase)
                                self.last_phase_change[tl_id] = current_time
                            if new_phase_added:
                                rl_agent.epsilon = min(0.5, rl_agent.epsilon + 0.1)
                                
                        self.last_green_time[self.lane_id_to_idx[most_starved_lane]] = current_time
                        self.debug_green_lanes(tl_id, lane_data)
                        continue
                    
                    # Collect intersection data for RL
                    self.tl_action_sizes[tl_id] = len(logic.phases)
                    queues = np.array([lane_data[l]['queue_length'] for l in controlled_lanes if l in lane_data])
                    waits = [lane_data[l]['waiting_time'] for l in controlled_lanes if l in lane_data]
                    speeds = [lane_data[l]['mean_speed'] for l in controlled_lanes if l in lane_data]
                    left_q = sum(lane_data[l]['queue_length'] for l in controlled_lanes 
                            if l in self.left_turn_lanes and l in lane_data)
                    right_q = sum(lane_data[l]['queue_length'] for l in controlled_lanes 
                                if l in self.right_turn_lanes and l in lane_data)
                    
                    self.intersection_data[tl_id] = {
                        'queues': queues, 'waits': waits, 'speeds': speeds,
                        'left_q': left_q, 'right_q': right_q,
                        'n_phases': self.tl_action_sizes[tl_id],
                        'current_phase': current_phase
                    }
                    
                    # RL phase control
                    if not hasattr(rl_agent, 'overwrite_enabled'):
                        rl_agent.overwrite_enabled = True
                        
                    if rl_agent.overwrite_enabled:
                        state = self._create_intersection_state_vector(tl_id, self.intersection_data)
                        phase_idx = rl_agent.select_and_apply_phase(state, adaptive_controller=apc)
                        self.last_phase_change[tl_id] = current_time
                        continue
                        
                    # Standard RL control
                    state = self._create_intersection_state_vector(tl_id, self.intersection_data)
                    action = rl_agent.get_action(state, tl_id, action_size=self.tl_action_sizes[tl_id])
                    last_change = self.last_phase_change.get(tl_id, -9999)
                    
                    if (current_time - last_change >= self.adaptive_params['min_green'] and
                        action != current_phase and
                        not self._is_in_dilemma_zone(tl_id, controlled_lanes, lane_data)):
                        
                        if not self._phase_has_traffic(logic, action, controlled_lanes, lane_data):
                            continue
                            
                        switched = self._switch_phase_with_yellow_if_needed(
                            tl_id, current_phase, action, logic, 
                            controlled_lanes, lane_data, current_time
                        )
                        if not switched:
                            apc.set_phase_from_API(action)
                            self.last_phase_change[tl_id] = current_time
                            self._process_rl_learning(self.intersection_data, lane_data, current_time)
                            
                    self.debug_green_lanes(tl_id, lane_data)
                    
                except Exception as e:
                    logger.error(f"[TL] Error processing {tl_id}: {e}")
                    continue
            
            # Step 6: Monitor congestion status periodically
            if self.step_count % 100 == 0:
                congestion_report = self.monitor_congestion_status()
                
                if congestion_report['clusters'] > 0:
                    logger.info(f"[CONGESTION SUMMARY] Step {self.step_count}: "
                            f"{congestion_report['clusters']} clusters, "
                            f"{congestion_report['active_responses']} active responses")
                    
                    # Check for network-wide congestion
                    critical_count = sum(1 for data in congestion_report['intersections'].values()
                                    if data['severity'] > 0.8)
                    if critical_count >= len(self.adaptive_phase_controllers) * 0.5:
                        logger.error(f"[EMERGENCY] Network congestion: {critical_count}/{len(self.adaptive_phase_controllers)} critical")
                        self.activate_emergency_congestion_mode()
            
            # Step 7: Coordinate arterial flow periodically
            if self.step_count % 300 == 0:  # Every 300 steps
                try:
                    self.coordinate_arterial_flow()
                except Exception as e:
                    logger.error(f"[ARTERIAL] Error coordinating arterial flow: {e}")
                    
            # Step 8: Check for phase restoration
            if hasattr(self, 'scheduled_restorations'):
                for tl_id, restore_time in list(self.scheduled_restorations.items()):
                    if current_time >= restore_time:
                        apc = self.adaptive_phase_controllers.get(tl_id)
                        if apc:
                            # Reset phase duration multipliers
                            apc.phase_duration_multiplier = defaultdict(lambda: 1.0)
                            logger.info(f"[RESTORE] Phase durations restored for {tl_id}")
                        del self.scheduled_restorations[tl_id]
                        
        except Exception as e:
            self.logger.error(f"Critical error in run_step: {e}", exc_info=True)
    def _adjust_traffic_lights(self, lane_data, lane_status, current_time):
        try:
            for tl_id in traci.trafficlight.getIDList():
                try:
                    cl = traci.trafficlight.getControlledLanes(tl_id)
                    for lane in cl: self.lane_to_tl[lane] = tl_id
                    if not self._handle_priority_conditions(tl_id, cl, lane_data, current_time):
                        self._perform_normal_control(tl_id, cl, lane_data, current_time)
                except Exception as e:
                    logger.info(f"Error adjusting traffic light {tl_id}: {e}")
        except Exception as e:
            logger.info(f"Error in _adjust_traffic_lights: {e}")
    def _execute_control_action(self, tl_id, target_lane, action, lane_data, current_time):
        try:
            if not isinstance(lane_data, dict) or target_lane not in lane_data:
                logger.info("⚠️ Invalid lane_data in _execute_control_action")
                return

            apc = self.adaptive_phase_controllers[tl_id]
            current_phase = traci.trafficlight.getPhase(tl_id)
            target_phase = self._find_phase_for_lane(tl_id, target_lane) or current_phase

            if current_phase != target_phase:
                # Apply via APC to ensure yellow
                phase_record = apc.load_phase_from_supabase(target_phase)
                dur = float(phase_record["duration"]) if phase_record else float(apc.max_green)
                safe_idx = self._safe_phase_index_controller(tl_id, target_phase)
                apc.set_phase_from_API(safe_idx, requested_duration=dur)

                self.last_phase_change[tl_id] = current_time
                self.last_green_time[self.lane_id_to_idx[target_lane]] = current_time
            elif action == 1:  # Next phase
                next_phase = (current_phase + 1) % self._get_phase_count(tl_id)
                apc.set_phase_from_API(next_phase, requested_duration=float(self.adaptive_params['min_green']))
                self.last_phase_change[tl_id] = current_time
            elif action == 2:  # Extend current phase
                try:
                    remaining = traci.trafficlight.getNextSwitch(tl_id) - current_time
                    extension = min(15.0, float(self.adaptive_params['max_green']) - remaining)
                    if extension > 0:
                        traci.trafficlight.setPhaseDuration(tl_id, remaining + extension)
                except Exception as e:
                    logger.info(f"Could not extend phase: {e}")
            elif action == 3:  # Shorten current phase
                try:
                    remaining = traci.trafficlight.getNextSwitch(tl_id) - current_time
                    if remaining > float(self.adaptive_params['min_green']) + 5.0:
                        reduction = min(5.0, remaining - float(self.adaptive_params['min_green']))
                        traci.trafficlight.setPhaseDuration(tl_id, remaining - reduction)
                except Exception as e:
                    logger.info(f"Could not shorten phase: {e}")
            elif action == 4:  # Balanced phase switch
                balanced_phase = self._get_balanced_phase(tl_id, lane_data)
                if balanced_phase != current_phase:
                    apc.set_phase_from_API(balanced_phase, requested_duration=float(self.adaptive_params['min_green']))
                    self.last_phase_change[tl_id] = current_time

            # Update phase utilization stats
            key = (tl_id, traci.trafficlight.getPhase(tl_id))
            self.phase_utilization[key] = self.phase_utilization.get(key, 0) + 1

        except Exception as e:
            logger.info(f"Error in _execute_control_action: {e}")    
    def _perform_normal_control(self, tl_id, controlled_lanes, lane_data, current_time):
        try:
            if not isinstance(lane_data, dict):
                logger.info(f"⚠️ lane_data is {type(lane_data)}, expected dict")
                return
            target = self._select_target_lane(tl_id, controlled_lanes, lane_data, current_time)
            if not target: return
            state = self._create_state_vector(target, lane_data)
            # PATCH: Use per-TLS RL agent
            rl_agent = self.rl_agents[tl_id] if hasattr(self, 'rl_agents') and tl_id in self.rl_agents else self.rl_agent
            if not rl_agent.is_valid_state(state): return
            action = rl_agent.get_action(state, lane_id=target)
            last_time = self.last_phase_change[tl_id] if isinstance(self.last_phase_change, dict) else 0
            if current_time - last_time >= 5:
                self._execute_control_action(tl_id, target, action, lane_data, current_time)
        except Exception as e:
            logger.info(f"Error in _perform_normal_control: {e}")
            traceback.logger.info_exc()
    # ========================================
    # 10. REINFORCEMENT LEARNING
    # ========================================          
    def _create_intersection_state_vector(self, tl_id, intersection_data):
        d = intersection_data[tl_id]
        queues = np.array(d.get('queues', []), dtype=float)
        waits = np.array(d.get('waits', []), dtype=float)
        speeds = np.array(d.get('speeds', []), dtype=float)
        n_phases = float(d.get('n_phases', 4))
        current_phase = float(d.get('current_phase', 0))
        state = np.array([
            queues.max() if queues.size else 0,
            queues.mean() if queues.size else 0,
            speeds.min() if speeds.size else 0,
            speeds.mean() if speeds.size else 0,
            waits.max() if waits.size else 0,
            waits.mean() if waits.size else 0,
            current_phase / max(n_phases - 1, 1), n_phases,
            float(d.get('left_q', 0)),
            float(d.get('right_q', 0))
        ])
        return state
    def _process_rl_learning(self, intersection_data, lane_data, current_time):
        try:
            for tl_id in intersection_data:
                if tl_id not in intersection_data: 
                    continue
                    
                d = intersection_data[tl_id]
                state = self._create_intersection_state_vector(tl_id, intersection_data)
                # PATCH: Use per-TLS RL agent
                rl_agent = self.rl_agents[tl_id] if hasattr(self, 'rl_agents') and tl_id in self.rl_agents else self.rl_agent
                if not rl_agent.is_valid_state(state): 
                    continue
                    
                queues, waits = d['queues'], d['waits']
                controlled_lanes = traci.trafficlight.getControlledLanes(tl_id)
                logic = self._get_traffic_light_logic(tl_id)
                current_phase = d['current_phase']
                logger.info(f"\n[RL STATE] TL: {tl_id}, Phase: {current_phase}")
                logger.info(f"  - Queues: {queues}")
                logger.info(f"  - Waits: {waits}")
                logger.info(f"  - Current phase state: {logic.phases[current_phase].state if logic else 'N/A'}")
                # Calculate empty green penalty
                empty_green_count = 0
                has_vehicle_on_green = False
                if logic:
                    phase_state = logic.phases[current_phase].state
                    for lane_idx, lane in enumerate(controlled_lanes):
                        if (lane_idx < len(phase_state) and phase_state[lane_idx].upper() == 'G'):
                            if lane in self.lane_id_to_idx and d['queues'][lane_idx] == 0:
                                empty_green_count += 1
                            if lane in self.lane_id_to_idx and d['queues'][lane_idx] > 0:
                                has_vehicle_on_green = True
                
                # Calculate congestion bonus
                congestion_bonus = sum(
                    min(self.adaptive_params['congestion_bonus'], q) 
                    for q in queues if q > 5
                )
                
                # Composite reward
                empty_green_penalty = self.adaptive_params['empty_green_penalty'] * empty_green_count
                only_empty_green_penalty = 0
                if not has_vehicle_on_green:
                    only_empty_green_penalty = 100  # make this large

                reward = (
                    -self.adaptive_params['queue_weight'] * sum(queues) 
                    - self.adaptive_params['wait_weight'] * sum(waits) 
                    - empty_green_penalty
                    - only_empty_green_penalty  # strong penalty!
                    + congestion_bonus
                )
                rl_agent.reward_history.append(reward)
                reward_components = {
                    "queue_penalty": -self.adaptive_params['queue_weight'] * sum(queues),
                    "wait_penalty": -self.adaptive_params['wait_weight'] * sum(waits),
                    "empty_green_penalty": -self.adaptive_params['empty_green_penalty'] * empty_green_count,
                    "congestion_bonus": congestion_bonus,
                    "total_raw": reward
                }
                logger.info(f"\n[RL REWARD COMPONENTS] TL: {tl_id}")
                logger.info(f"  - Queue penalty: {reward_components['queue_penalty']:.2f}")
                logger.info(f"  - Wait penalty: {reward_components['wait_penalty']:.2f}")
                logger.info(f"  - Empty green penalty: {reward_components['empty_green_penalty']:.2f}")
                logger.info(f"  - Congestion bonus: {reward_components['congestion_bonus']:.2f}")
                logger.info(f"  - TOTAL REWARD: {reward:.2f}")
                # Update Q-table if we have previous state
                if tl_id in self.previous_states and tl_id in self.previous_actions:
                    prev_state, prev_action = self.previous_states[tl_id], self.previous_actions[tl_id]
                    rl_agent.update_q_table(
                        prev_state, prev_action, reward, state, 
                        tl_id=tl_id, 
                        extra_info={
                            **reward_components,
                            'episode': self.current_episode,
                            'simulation_time': current_time,
                            'action_name': rl_agent._get_action_name(prev_action),
                            'queue_length': max(queues) if queues else 0,
                            'waiting_time': max(waits) if waits else 0,
                            'mean_speed': np.mean(d['speeds']) if d['speeds'] else 0,
                            'left_turn': d['left_q'], 'right_turn': d['right_q'],
                            'phase_id': current_phase
                        },
                        action_size=d['n_phases']
                    )
                
                # Store current state/action for next step
                action = rl_agent.get_action(state, tl_id=tl_id)
                self.previous_states[tl_id], self.previous_actions[tl_id] = state, action
                
        except Exception as e:
            logger.info(f"Error in _process_rl_learning: {e}")
            traceback.logger.info_exc()
    def _create_state_vector(self, lane_id, lane_data):
        try:
            if not (isinstance(lane_data, dict) and lane_id in lane_data):
                return np.zeros(self.rl_agent.state_size)
            d = lane_data[lane_id]
            tl_id = self.lane_to_tl.get(lane_id, "")
            norm = lambda x, b: min(x / b, 1.0)
            qn, wn, dn, sn, fn = norm(d['queue_length'], self.norm_bounds['queue']), norm(d['waiting_time'], self.norm_bounds['wait']), norm(d['density'], self.norm_bounds['density']), norm(d['mean_speed'], self.norm_bounds['speed']), norm(d['flow'], self.norm_bounds['flow'])
            d['arrival_rate'] = d.get('arrival_rate', self._calculate_arrival_rate(lane_id))
            an = norm(d['arrival_rate'], self.norm_bounds['arrival_rate'])
            rqn = norm(d.get('queue_route', 0), self.norm_bounds['queue'] * 3)
            rfn = norm(d.get('flow_route', 0), self.norm_bounds['flow'] * 3)
            current_phase, phase_norm, phase_eff = 0, 0.0, 0.0
            if tl_id:
                try:
                    current_phase = traci.trafficlight.getPhase(tl_id)
                    num_phases = self._get_phase_count(tl_id)
                    phase_norm = current_phase / max(num_phases-1, 1)
                    phase_eff = self._get_phase_efficiency(tl_id, current_phase)
                except: pass
            last_green = self.last_green_time[self.lane_id_to_idx[lane_id]]
            tsg = norm(traci.simulation.getTime() - last_green, self.norm_bounds['time_since_green'])
            state = np.array([
                qn, wn, dn, sn, fn, rqn, rfn, phase_norm, tsg,
                float(d['ambulance']), self.lane_scores[self.lane_id_to_idx[lane_id]] / 100, phase_eff
            ])
            return np.nan_to_num(state, nan=0.0, posinf=1.0, neginf=0.0)
        except Exception as e:
            logger.info(f"Error creating state vector for {lane_id}: {e}")
            return np.zeros(self.rl_agent.state_size)
    def _calculate_reward(self, lane_id, lane_data, action_taken, current_time):
        try:
            if not (isinstance(lane_data, dict) and lane_id in lane_data):
                return 0.0, {}, 0.0
            d = lane_data[lane_id]
            lt_factor = 1.5 if d['left_turn'] else 1.0
            qp = -min(d['queue_length'] * self.adaptive_params['queue_weight'] * lt_factor, 30)
            wp = -min(d['waiting_time'] * self.adaptive_params['wait_weight'] * lt_factor, 20)
            tr = min(d['flow'] * self.adaptive_params['flow_weight'], 25)
            sr = min(d['mean_speed'] * self.adaptive_params['speed_weight'], 15)
            ltb = 15 if d['left_turn'] and action_taken == 0 and d['queue_length'] < 2 else 0
            ab = min(d['queue_length'] * 0.7, 20) if action_taken == 0 and d['queue_length'] > 5 else 0
            lg = self.last_green_time[self.lane_id_to_idx[lane_id]]
            sp = -min(30, (current_time-lg-self.adaptive_params['starvation_threshold'])*0.5) if current_time-lg > self.adaptive_params['starvation_threshold'] else 0
            ambb = 25 if d['ambulance'] else 0
            eb = 15 if d['queue_length'] < 3 and d['mean_speed'] > 5 else 0
            total = qp + wp + tr + sr + ab + sp + ambb + eb
            norm_reward = np.clip(total / self.adaptive_params['reward_scale'], -1.0, 1.0)
            if np.isnan(norm_reward) or np.isinf(norm_reward): norm_reward = 0.0
            comps = {'queue_penalty': qp, 'wait_penalty': wp, 'throughput_reward': tr, 'speed_reward': sr, 'action_bonus': ab, 'starvation_penalty': sp, 'ambulance_bonus': ambb, 'total_raw': total, 'normalized': norm_reward}
            return norm_reward, comps, total
        except Exception as e:
            logger.info(f"Error calculating reward for {lane_id}: {e}")
            return 0.0, {}, 0.0
    def end_episode(self):
        for tls_id, rl_agent in self.rl_agents.items():
            if rl_agent.reward_history:
                avg_reward = np.mean(rl_agent.reward_history)
                logger.info(f"[{tls_id}] Average reward this episode: {avg_reward:.2f}")
            rl_agent.save_model(adaptive_params=self.adaptive_params)
            old_epsilon = rl_agent.epsilon
            rl_agent.epsilon = max(rl_agent.epsilon * rl_agent.epsilon_decay, rl_agent.min_epsilon)
            logger.info(f"[{tls_id}] Epsilon after training/episode: {old_epsilon} -> {rl_agent.epsilon}")
            rl_agent.reward_history.clear()
    def _update_adaptive_parameters(self, performance_stats):
        try:
            avg_reward = performance_stats.get('avg_reward', 0)
            if avg_reward > 0.6:
                self.adaptive_params['min_green'] = min(15, self.adaptive_params['min_green'] + 1)
                self.adaptive_params['max_green'] = min(90, self.adaptive_params['max_green'] + 5)
            elif avg_reward < 0.3:
                self.adaptive_params['min_green'] = max(5, self.adaptive_params['min_green'] - 1)
                self.adaptive_params['max_green'] = max(30, self.adaptive_params['max_green'] - 5)
            logger.info("🔄 Updated adaptive parameters:", self.adaptive_params)
        except Exception as e:
            logger.info(f"Error updating adaptive parameters: {e}")
def main():
    parser = argparse.ArgumentParser(description="Run universal SUMO RL traffic simulation")
    parser.add_argument('--sumo', required=True, help='Path to SUMO config file')
    parser.add_argument('--gui', action='store_true', help='Use SUMO GUI')
    parser.add_argument('--max-steps', type=int, help='Maximum simulation steps per episode')
    parser.add_argument('--episodes', type=int, default=1, help='Number of episodes to run')
    parser.add_argument('--num-retries', type=int, default=1, help='Number of retries if connection fails')
    parser.add_argument('--retry-delay', type=int, default=1, help='Delay in seconds between retries')
    parser.add_argument('--mode', choices=['train', 'eval', 'adaptive'], default='train',
                        help='Controller mode: train (explore+learn), eval (exploit only), adaptive (exploit+learn)')
    parser.add_argument('--api', action='store_true', help='Start API server instead of simulation')
    args = parser.parse_args()
    start_universal_simulation(
        sumocfg_path=args.sumo,
        use_gui=args.gui,
        max_steps=args.max_steps,
        episodes=args.episodes,
        num_retries=args.num_retries,
        retry_delay=args.retry_delay,
        mode=args.mode)

def start_universal_simulation(sumocfg_path, use_gui=True, max_steps=None, 
                              episodes=1, num_retries=1, retry_delay=1, mode="train"):
    """Start simulation with proper corridor coordinator initialization"""
    global controller
    controller = None

    def simulation_loop():
        global controller
        try:
            for episode in range(episodes):
                logger.info(f"\n{'='*50}\n🚦 STARTING UNIVERSAL EPISODE {episode + 1}/{episodes}\n{'='*50}")
                
                # Start SUMO
                sumo_binary = "sumo-gui" if use_gui else "sumo"
                sumo_cmd = [
                    os.path.join(os.environ['SUMO_HOME'], 'bin', sumo_binary),
                    '-c', sumocfg_path, '--start', '--quit-on-end'
                ]
                
                # Add retry logic for SUMO connection
                for retry in range(num_retries):
                    try:
                        traci.start(sumo_cmd)
                        logger.info("✅ Connected to SUMO successfully")
                        break
                    except Exception as e:
                        if retry < num_retries - 1:
                            logger.warning(f"Connection attempt {retry + 1} failed: {e}")
                            time.sleep(retry_delay)
                        else:
                            raise e
                
                try:
                    # Install global safety guards for TraCI
                    _real_setPhase = traci.trafficlight.setPhase
                    def _patched_setPhase(tls_id, idx):
                        try:
                            logic = get_current_logic(tls_id)
                            n = len(logic.getPhases()) if logic else 0
                            if n == 0:
                                logger.warning(f"[GUARD] No phases for {tls_id}, skipping setPhase")
                                return
                            safe_idx = max(0, min(int(idx), n - 1))
                            if safe_idx != idx:
                                logger.info(f"[GUARD] Clamping setPhase({idx}) -> {safe_idx} for {tls_id} (n={n})")
                            return _real_setPhase(tls_id, safe_idx)
                        except Exception as e:
                            logger.error(f"[GUARD] setPhase failed: {e}")
                            return None
                    traci.trafficlight.setPhase = _patched_setPhase
                    
                    _real_setDef = traci.trafficlight.setCompleteRedYellowGreenDefinition
                    def _patched_setDef(tls_id, logic):
                        try:
                            phases = logic.getPhases() if hasattr(logic, "getPhases") else getattr(logic, "phases", [])
                            n = len(phases) if phases is not None else 0
                            if n > 0:
                                cpi = getattr(logic, "currentPhaseIndex", 0)
                                safe_cpi = max(0, min(int(cpi), n - 1))
                                if safe_cpi != cpi:
                                    logger.info(f"[GUARD] Clamping currentPhaseIndex {cpi} -> {safe_cpi} for {tls_id}")
                                    logic = traci.trafficlight.Logic(
                                        programID=getattr(logic, "programID", ""),
                                        type=getattr(logic, "type", 0),
                                        currentPhaseIndex=safe_cpi,
                                        phases=phases
                                    )
                        except Exception as e:
                            logger.warning(f"[GUARD] setCompleteRedYellowGreenDefinition guard failed: {e}")
                        return _real_setDef(tls_id, logic)
                    traci.trafficlight.setCompleteRedYellowGreenDefinition = _patched_setDef
                    
                except Exception as e:
                    logger.warning(f"[GUARD] Could not install TraCI guards: {e}")
                
                # Create controller
                controller = UniversalSmartTrafficController(sumocfg_path=sumocfg_path, mode=mode)
                controller.initialize()
                
                # Initialize corridor coordinator after controller is ready
                if not controller.corridor and len(controller.adaptive_phase_controllers) > 0:
                    # Configure improved coordinator with optimized parameters
                    coordinator_config = {
                        'spillback_threshold': 0.7,  # More sensitive spillback detection
                        'congestion_threshold': 0.5,  # Earlier congestion detection
                        'arterial_angle_tolerance': 30,  # Degrees for arterial alignment
                        'grid_angle_tolerance': 15,  # Stricter grid detection
                        'dbscan_eps': 300,  # 300m clustering radius for congestion
                        'dbscan_min_samples': 2,  # Minimum cluster size
                        'green_wave_speed': 13.89,  # 50 km/h default progression
                        'priority_horizon': 120,  # 2 minute look-ahead for priority
                        'coordination_update_interval': 30,  # Update groups every 30s
                    }
                    
                    controller.corridor = ImprovedCorridorCoordinator(
                        controller,
                        config=coordinator_config
                    )
                    
                    # Pass coordinator to RL agents
                    for tl_id, rl_agent in controller.rl_agents.items():
                        rl_agent.coordinator = controller.corridor
                    
                    logger.info(f"[CORRIDOR] Initialized improved coordinator for "
                              f"{len(controller.adaptive_phase_controllers)} intersections")
                
                # Build network topology with enhanced detection
                if controller.corridor:
                    controller.corridor.update_topology(force=True)
                    
                    # Perform initial group detection
                    detected_groups = controller.corridor.detect_intersection_groups_improved()
                    
                    logger.info("[CORRIDOR] Network topology built with enhanced algorithms")
                    
                    # Log detected groups
# Log detected groups
                    if detected_groups:
                        arterial_count = sum(1 for g in detected_groups.values() if g.group_type == 'arterial')
                        grid_count = sum(1 for g in detected_groups.values() if g.group_type == 'grid')
                        congestion_count = sum(1 for g in detected_groups.values() if g.group_type == 'congestion')
                        
                        logger.info(f"[TOPOLOGY] Initial detection: {arterial_count} arterials, "
                                f"{grid_count} grids, {congestion_count} congestion clusters")                
                # Build network topology
                if controller.corridor:
                    controller.corridor.update_topology(force=True)
                    logger.info("[CORRIDOR] Network topology built")
                    
                    # Log network structure
                    total_connections = 0
                    for tl_id in controller.adaptive_phase_controllers:
                        upstream = controller.corridor._upstream_tls.get(tl_id, set())
                        downstream = controller.corridor._downstream_tls.get(tl_id, set())
                        total_connections += len(upstream) + len(downstream)
                        if upstream or downstream:
                            logger.debug(f"[TOPOLOGY] {tl_id}: {len(upstream)} upstream, {len(downstream)} downstream")
                    
                    logger.info(f"[TOPOLOGY] Total network connections: {total_connections}")
                
                # Initialize controller phases if needed
                if hasattr(controller, "initialize_controller_phases"):
                    logger.info("[INIT] Setting up controller-managed phases")
                    controller.initialize_controller_phases()
                    
                    # Log phase configuration
                    for tl_id in traci.trafficlight.getIDList():
                        logic = traci.trafficlight.getAllProgramLogics(tl_id)[0]
                        logger.debug(f"[PHASES] {tl_id}: {len(logic.phases)} phases configured")
                
                controller.current_episode = episode + 1
                
                # Main simulation loop (PATCHED: step SUMO first, then control; graceful shutdown)
                step = 0
                tstart = time.time()
                last_corridor_log = 0
                congestion_events = 0

                def _traci_alive():
                    try:
                        _ = traci.simulation.getMinExpectedNumber()
                        return True
                    except Exception:
                        return False
                
                while True:
                    # Stop if SUMO has ended
                    if not _traci_alive():
                        break

                    # Respect max_steps if provided
                    if max_steps and step >= max_steps:
                        logger.info(f"Reached max steps ({max_steps}), ending episode.")
                        break

                    # Advance simulation first
                    try:
                        traci.simulationStep()
                    except Exception as e:
                        logger.error(f"[SIM] simulationStep failed or SUMO closed: {e}")
                        break

                    # If no more vehicles are expected, allow one guarded controller step to flush state
                    try:
                        if traci.simulation.getMinExpectedNumber() <= 0:
                            if controller and hasattr(controller, 'run_step'):
                                try:
                                    controller.run_step()
                                except Exception:
                                    pass
                            break
                    except Exception:
                        # If we cannot query, assume SUMO closed
                        break

                    # Normal controller step on fresh state
                    try:
                        controller.run_step()
                    except Exception as e:
                        logger.error(f"[CTRL] run_step failed: {e}")
                        if not _traci_alive():
                            break

                    # Early imbalance check (after controller step)
                    if step < 100 and step % 5 == 0:
                        for tl_id, apc in controller.adaptive_phase_controllers.items():
                            try:
                                # Quick check for obvious imbalances
                                lanes = apc.lane_ids
                                empty_count = sum(1 for l in lanes if traci.lane.getLastStepVehicleNumber(l) == 0)
                                busy_count = sum(1 for l in lanes if traci.lane.getLastStepHaltingNumber(l) > 5)
                                
                                if empty_count > 0 and busy_count > 0:
                                    logger.warning(f"[EARLY CHECK] {tl_id} at step {step}: "
                                                f"{empty_count} empty lanes, {busy_count} busy lanes")
                                    
                                    # Force phase change to serve busy lanes
                                    for lane in lanes:
                                        if traci.lane.getLastStepHaltingNumber(lane) > 5:
                                            phase = apc.find_or_create_phase_for_lane(lane)
                                            if phase is not None:
                                                apc.set_phase_from_API(phase, requested_duration=30)
                                                logger.info(f"[EARLY FIX] Forced phase {phase} for busy lane {lane}")
                                                break
                            except Exception:
                                continue

                    step += 1
                    
                    # Periodic corridor performance logging
                    if controller.corridor and step - last_corridor_log >= 500:
                        clusters = controller.corridor._congestion_clusters
                        responses = controller.corridor._active_responses
                        
                        if clusters or responses:
                            logger.info(f"[CORRIDOR STATUS] Step {step}: "
                                      f"{len(clusters)} congestion clusters, "
                                      f"{len(responses)} active responses")
                            
                            # Log cluster details
                            for i, cluster in enumerate(clusters):
                                severities = [controller.corridor._calculate_tl_congestion_severity(tl) 
                                            for tl in cluster]
                                avg_severity = np.mean(severities)
                                logger.info(f"  Cluster {i+1}: {len(cluster)} TLs, avg severity: {avg_severity:.2f}")
                            
                            # Count congestion events
                            if clusters:
                                congestion_events += 1
                        
                        last_corridor_log = step
                    
                    # Regular progress logging
                    if step % 1000 == 0:
                        elapsed = time.time() - tstart
                        step_rate = step / elapsed if elapsed > 0 else 0
                        
                        # Collect network statistics
                        total_waiting = sum(traci.lane.getWaitingTime(lane) 
                                          for lane in controller.lane_id_list)
                        total_halting = sum(traci.lane.getLastStepHaltingNumber(lane) 
                                          for lane in controller.lane_id_list)
                        
                        logger.info(f"Episode {episode + 1}: Step {step} | "
                                  f"Rate: {step_rate:.1f} steps/s | "
                                  f"Waiting: {total_waiting:.0f}s | "
                                  f"Halting: {total_halting:.0f} vehicles | "
                                  f"Congestion events: {congestion_events}")
                
                # Episode summary
                logger.info(f"\n{'='*50}")
                logger.info(f"Episode {episode + 1} completed after {step} steps")
                logger.info(f"Total congestion events: {congestion_events}")
                
                if controller.corridor:
                    # Log final corridor statistics
                    total_responses = sum(1 for tl in controller.adaptive_phase_controllers 
                                        if tl in controller.corridor._response_effectiveness)
                    avg_effectiveness = np.mean(list(controller.corridor._response_effectiveness.values())) \
                                      if controller.corridor._response_effectiveness else 0
                    
                    logger.info(f"Corridor responses applied: {total_responses}")
                    logger.info(f"Average effectiveness: {avg_effectiveness:.2f}")
                
                logger.info(f"{'='*50}\n")
                
                # Save models and clean up
                controller.end_episode()
                traci.close()
                
                # Inter-episode delay
                if episode < episodes - 1:
                    time.sleep(2)
                    
            
        except Exception as e:
            logger.error(f"Fatal error in simulation: {e}", exc_info=True)
            
        finally:
            try:
                traci.close()
            except:
                pass
            logger.info("Simulation resources cleaned up")
    
    # Start simulation in thread
    sim_thread = threading.Thread(target=simulation_loop)
    sim_thread.start()
    
    # Wait for controller initialization
    while controller is None or not hasattr(controller, "adaptive_phase_controllers"):
        time.sleep(0.1)
    
    # Wait for SUMO to be ready
    while True:
        try:
            if traci.trafficlight.getIDList():
                break
        except:
            pass
        time.sleep(0.1)
    
    # Start display
    display = SmartIntersectionTrafficDisplay(
        controller.phase_events, 
        controller=controller, 
        poll_interval=1
    )
    display.start()
    
    # Wait for simulation to complete
    sim_thread.join()
    
    # Stop display
    display.stop()
    
    # Return controller for analysis
    return controller
if __name__ == "__main__":
    main()