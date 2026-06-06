"""
Trace Service - Unified trace recording with SQLAlchemy
Decoupled from Graph logic for clean separation
"""

import time
from datetime import datetime
from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from backend.models import TraceStep


@dataclass
class TraceStepRecord:
    """
    Immutable trace step record.
    Used to pass trace data through the graph without DB coupling.
    """
    step_type: str
    step_order: int
    input_data: Dict = field(default_factory=dict)
    output_data: Dict = field(default_factory=dict)
    duration_ms: int = 0
    status: str = "SUCCESS"
    error_message: Optional[str] = None
    created_at: datetime = field(default_factory=datetime.now)
    
    def to_dict(self) -> Dict:
        """Convert to dictionary for serialization."""
        return {
            "step_type": self.step_type,
            "step_order": self.step_order,
            "input_data": self.input_data,
            "output_data": self.output_data,
            "duration_ms": self.duration_ms,
            "status": self.status,
            "error_message": self.error_message,
        }


class TraceService:
    """
    Trace recording service.
    
    Features:
    - Deferred batch writes for performance
    - Structured logging integration
    - Step order management
    """
    
    def __init__(
        self,
        db: AsyncSession,
        task_id: int,
        initial_step_order: int = 0,
        iteration: int = 1,
        run_id: int | None = None,
    ):
        self.db = db
        self.task_id = task_id
        self.iteration = iteration
        self.run_id = run_id
        self.current_step_order = initial_step_order
        self._pending_records: List[TraceStepRecord] = []
        
        logger.info(f"[[DEBUG_TRACE]] TraceService Initialized | ID={id(self)} task_id={task_id} iter={iteration}")
        logger.debug(f"[TraceService] Initialized | task_id={task_id} iter={iteration}")
    
    def create_record(
        self,
        step_type: str,
        input_data: Dict = None,
        output_data: Dict = None,
        duration_ms: int = 0,
        status: str = "SUCCESS",
        error_message: str = None
    ) -> TraceStepRecord:
        """
        Create a new trace record (does not persist yet).
        Auto-increments step_order.
        """
        self.current_step_order += 1
        
        record = TraceStepRecord(
            step_type=step_type,
            step_order=self.current_step_order,
            input_data=input_data or {},
            output_data=output_data or {},
            duration_ms=duration_ms,
            status=status,
            error_message=error_message
        )
        
        self._pending_records.append(record)
        
        logger.info(
            f"[TraceService] Record created | "
            f"task={self.task_id} step={self.current_step_order} "
            f"type={step_type} status={status}"
        )
        
        return record
    
    async def persist_record(self, record: TraceStepRecord) -> Optional[TraceStep]:
        """
        Persist a single trace record to database.
        Returns the created TraceStep model, or None if persistence failed.
        """
        try:
            trace_step = TraceStep(
                task_id=self.task_id,
                step_type=record.step_type,
                step_order=record.step_order,
                iteration=self.iteration,
                input_data=record.input_data,
                output_data=record.output_data,
                duration_ms=record.duration_ms,
                status=record.status,
                error_message=record.error_message
            )
            
            logger.info(f"[[DEBUG_TRACE]] Persisting Record | type={record.step_type} iter={self.iteration}")
            
            self.db.add(trace_step)
            await self.db.flush()
            await self.db.commit()  # Force commit for real-time visibility
            
            logger.debug(
                f"[TraceService] Record persisted | "
                f"id={trace_step.id} step={record.step_order} iter={self.iteration}"
            )
            
            return trace_step
            
        except Exception as e:
            logger.warning(f"[TraceService] Failed to persist record: {e}")
            # Rollback to recover from failed transaction
            try:
                await self.db.rollback()
            except Exception:
                pass
            return None
    
    async def flush_all(self) -> List[TraceStep]:
        """
        Persist all pending records to database.
        Returns list of created TraceStep models.
        """
        if not self._pending_records:
            return []
        
        try:
            trace_steps = []
            for record in self._pending_records:
                trace_step = TraceStep(
                    task_id=self.task_id,
                    step_type=record.step_type,
                    step_order=record.step_order,
                    iteration=self.iteration,
                    input_data=record.input_data,
                    output_data=record.output_data,
                    duration_ms=record.duration_ms,
                    status=record.status,
                    error_message=record.error_message
                )
                self.db.add(trace_step)
                trace_steps.append(trace_step)
            
            await self.db.flush()
            
            logger.info(
                f"[TraceService] Flushed {len(trace_steps)} records | task={self.task_id}"
            )
            
            self._pending_records.clear()
            return trace_steps
            
        except Exception as e:
            logger.warning(f"[TraceService] Failed to flush records: {e}")
            # Rollback to recover from failed transaction
            try:
                await self.db.rollback()
            except Exception:
                pass
            self._pending_records.clear()
            return []
    
    def get_pending_records(self) -> List[TraceStepRecord]:
        """Get list of pending (not yet persisted) records."""
        return self._pending_records.copy()
    
    @property
    def step_order(self) -> int:
        """Get current step order."""
        return self.current_step_order


class TraceContext:
    """
    Context manager for timing and tracing a step.
    
    Usage:
        async with TraceContext(trace_service, "CODE_GEN", {"input": data}) as ctx:
            result = await do_something()
            ctx.set_output(result)
    """
    
    def __init__(
        self,
        trace_service: TraceService,
        step_type: str,
        input_data: Dict = None
    ):
        self.trace_service = trace_service
        self.step_type = step_type
        self.input_data = input_data or {}
        self.output_data = {}
        self.status = "SUCCESS"
        self.error_message = None
        self.start_time = None
        self.record: Optional[TraceStepRecord] = None
    
    async def __aenter__(self):
        self.start_time = time.time()
        logger.debug(f"[TraceContext] Started | type={self.step_type}")
        return self
    
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        duration_ms = int((time.time() - self.start_time) * 1000)
        
        if exc_type is not None:
            self.status = "FAILED"
            self.error_message = str(exc_val)
        
        self.record = self.trace_service.create_record(
            step_type=self.step_type,
            input_data=self.input_data,
            output_data=self.output_data,
            duration_ms=duration_ms,
            status=self.status,
            error_message=self.error_message
        )
        
        # Don't suppress exceptions
        return False
    
    def set_output(self, output: Dict):
        """Set output data for the trace record."""
        self.output_data = output
    
    def set_error(self, error: str):
        """Mark step as failed with error message."""
        self.status = "FAILED"
        self.error_message = error
