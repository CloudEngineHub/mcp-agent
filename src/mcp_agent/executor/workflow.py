import asyncio
import sys

from abc import ABC, abstractmethod
from datetime import datetime, timezone
from typing import (
    Any,
    Dict,
    Generic,
    Optional,
    TypeVar,
    TYPE_CHECKING,
)


from pydantic import BaseModel, ConfigDict, Field
from mcp_agent.core.context_dependent import ContextDependent
from mcp_agent.executor.temporal import TemporalExecutor
from mcp_agent.executor.temporal.workflow_signal import (
    SignalMailbox,
    TemporalSignalHandler,
)
from mcp_agent.executor.workflow_signal import Signal
from mcp_agent.logging.logger import get_logger

if TYPE_CHECKING:
    from temporalio.client import WorkflowHandle
    from mcp_agent.core.context import Context

T = TypeVar("T")


class WorkflowState(BaseModel):
    """
    Simple container for persistent workflow state.
    This can hold fields that should persist across tasks.
    """

    # TODO: saqadri - (MAC) - This should be a proper status enum
    status: str = "initialized"
    metadata: Dict[str, Any] = Field(default_factory=dict)
    updated_at: float | None = None
    error: Dict[str, Any] | None = None

    model_config = ConfigDict(extra="allow", arbitrary_types_allowed=True)

    def record_error(self, error: Exception) -> None:
        self.error = {
            "type": type(error).__name__,
            "message": str(error),
            "timestamp": datetime.now(timezone.utc).timestamp(),
        }


class WorkflowResult(BaseModel, Generic[T]):
    value: Optional[T] = None
    metadata: Dict[str, Any] = Field(default_factory=dict)
    start_time: float | None = None
    end_time: float | None = None


class WorkflowExecution(BaseModel):
    """
    Represents a workflow execution with its run ID and workflow ID.
    This is used to track the execution of workflows.
    """

    workflow_id: str
    run_id: str | None = None


class Workflow(ABC, Generic[T], ContextDependent):
    """
    Base class for user-defined workflows.
    Handles execution and state management.

    Workflows represent user-defined application logic modules that can use Agents and AugmentedLLMs.
    Typically, workflows are registered with an MCPApp and can be exposed as MCP tools via app_server.py.

    Some key notes:
        - The class MUST be decorated with @app.workflow.
        - Persistent state: Provides a simple `state` object for storing data across tasks.
        - Lifecycle management: Provides run_async, pause, resume, cancel, and get_status methods.
    """

    def __init__(
        self,
        name: str | None = None,
        metadata: Dict[str, Any] | None = None,
        context: Optional["Context"] = None,
        **kwargs: Any,
    ):
        # Initialize the ContextDependent mixin
        ContextDependent.__init__(self, context=context)

        self.name = name or self.__class__.__name__
        self._logger = get_logger(f"workflow.{self.name}")
        self._initialized = False
        self._workflow_id = None  # Will be set during run_async
        self._run_id = None  # Will be set during run_async
        self._run_task = None

        # A simple workflow state object
        # If under Temporal, storing it as a field on this class
        # means it can be replayed automatically
        self.state = WorkflowState(metadata=metadata or {})

        # Flag to prevent re-attaching signals
        # Set in signal_handler.attach_to_workflow (done in workflow initialize())
        self._signal_handler_attached = False
        self._signal_mailbox: SignalMailbox = SignalMailbox()

    @property
    def executor(self):
        """Get the workflow executor from the context."""
        executor = self.context.executor
        if executor is None:
            raise ValueError("No executor available in context")
        return executor

    @property
    def id(self) -> str | None:
        """
        Get the workflow ID for this workflow.
        """
        return self._workflow_id

    @property
    def run_id(self) -> str | None:
        """
        Get the workflow run ID if it has been assigned.
        NOTE: The run() method will assign a new workflow ID on every run.
        """
        return self._run_id

    @classmethod
    async def create(
        cls, name: str | None = None, context: Optional["Context"] = None, **kwargs
    ) -> "Workflow":
        """
        Factory method to create and initialize a workflow instance.

        This default implementation creates a workflow instance and calls initialize().
        Subclasses can override this method for custom initialization logic.

        Args:
            name: Optional name for the workflow (defaults to class name)
            context: Optional context to use (falls back to global context if not provided)
            **kwargs: Additional parameters to pass to the workflow constructor

        Returns:
            An initialized workflow instance
        """
        workflow = cls(name=name, context=context, **kwargs)
        await workflow.initialize()
        return workflow

    @abstractmethod
    async def run(self, *args, **kwargs) -> "WorkflowResult[T]":
        """
        Main workflow implementation. Must be overridden by subclasses.

        This is where the user-defined application logic goes. Typically, this involves:
        1. Setting up Agents and attaching LLMs to them
        2. Executing operations using the Agents and their LLMs
        3. Processing results and returning them

        Returns:
            WorkflowResult containing the output of the workflow
        """

    async def _cancel_task(self):
        """
        Wait for a cancel signal and cancel the workflow task.
        """
        signal = await self.executor.wait_for_signal(
            "cancel",
            workflow_id=self.id,
            run_id=self.run_id,
            signal_description="Waiting for cancel signal",
        )

        self._logger.info(f"Cancel signal received for workflow run {self._run_id}")
        self.update_status("cancelling")

        # The run task will be cancelled in the run_async method
        return signal

    async def run_async(self, *args, **kwargs) -> "WorkflowExecution":
        """
        Run the workflow asynchronously and return the WorkflowExecution.

        This creates an async task that will be executed through the executor
        and returns immediately with a WorkflowExecution with run ID that can
        be used to check status, resume, or cancel.

        Args:
            *args: Positional arguments to pass to the run method
            **kwargs: Keyword arguments to pass to the run method
                Special kwargs that are extracted and not passed to run():
                - __mcp_agent_workflow_id: Optional workflow ID to use (instead of auto-generating)
                - __mcp_agent_task_queue: Optional task queue to use (instead of default from config)

        Returns:
            WorkflowExecution: The execution details including run ID and workflow ID
        """

        import asyncio
        from concurrent.futures import CancelledError

        handle: "WorkflowHandle" | None = None

        # Extract special kwargs that shouldn't be passed to the run method
        # Using __mcp_agent_ prefix to avoid conflicts with user parameters
        provided_workflow_id = kwargs.pop("__mcp_agent_workflow_id", None)
        provided_task_queue = kwargs.pop("__mcp_agent_task_queue", None)

        self.update_status("scheduled")

        if self.context.config.execution_engine == "asyncio":
            # Generate a unique ID for this workflow instance
            if not self._workflow_id:
                self._workflow_id = provided_workflow_id or self.name
            if not self._run_id:
                self._run_id = str(self.executor.uuid())
        elif self.context.config.execution_engine == "temporal":
            # For Temporal workflows, we'll start the workflow immediately
            executor: TemporalExecutor = self.executor
            handle = await executor.start_workflow(
                self.name,
                *args,
                workflow_id=provided_workflow_id,
                task_queue=provided_task_queue,
                **kwargs,
            )
            self._workflow_id = handle.id
            self._run_id = handle.result_run_id or handle.run_id
        else:
            raise ValueError(
                f"Unsupported execution engine: {self.context.config.execution_engine}"
            )

        self._logger.debug(
            f"Workflow started with workflow ID: {self._workflow_id}, run ID: {self._run_id}"
        )

        # Define the workflow execution function
        async def _execute_workflow():
            try:
                # Push token tracking context if available
                pushed_token_context = False
                if self.context and self.context.token_counter:
                    try:
                        self.context.token_counter.push(
                            name=self.name,
                            node_type="workflow",
                            metadata={
                                "workflow_id": self._workflow_id,
                                "run_id": self._run_id,
                                "class": self.__class__.__name__,
                            },
                        )
                        pushed_token_context = True
                    except Exception as e:
                        self._logger.error(f"Error pushing token context: {e}")

                # Run the workflow through the executor with pause/cancel monitoring
                self.update_status("running")

                tasks = []
                cancel_task = None
                if self.context.config.execution_engine == "temporal" and handle:
                    run_task = asyncio.create_task(handle.result())
                    # TODO: jerron - cancel task not working for temporal
                    tasks.append(run_task)
                else:
                    run_task = asyncio.create_task(self.run(*args, **kwargs))
                    cancel_task = asyncio.create_task(self._cancel_task())
                    tasks.extend([run_task, cancel_task])

                # Simply wait for either the run task or cancel task to complete
                try:
                    # Wait for either task to complete, whichever happens first
                    done, _ = await asyncio.wait(
                        tasks,
                        return_when=asyncio.FIRST_COMPLETED,
                    )

                    # Check which task completed
                    if cancel_task in done:
                        # Cancel signal received, cancel the run task
                        run_task.cancel()
                        self.update_status("cancelled")
                        raise CancelledError("Workflow was cancelled")
                    elif run_task in done:
                        # Run task completed, cancel the cancel task
                        if cancel_task:
                            cancel_task.cancel()
                        # Get the result (or propagate any exception)
                        result = await run_task
                        self.update_status("completed")
                        return result

                except Exception as e:
                    self._logger.error(f"Error waiting for tasks: {e}")
                    raise

            except CancelledError:
                # Handle cancellation gracefully
                self._logger.info(
                    f"Workflow {self.name} (ID: {self._run_id}) was cancelled"
                )
                self.update_status("cancelled")
                raise
            except Exception as e:
                # Log and propagate exceptions
                self._logger.error(
                    f"Error in workflow {self.name} (ID: {self._run_id}): {str(e)}"
                )
                self.update_status("error")
                self.state.record_error(e)
                raise
            finally:
                try:
                    # Pop token context if we pushed it
                    if (
                        pushed_token_context
                        and self.context
                        and self.context.token_counter
                    ):
                        try:
                            self.context.token_counter.pop()
                        except Exception as e:
                            self._logger.error(f"Error popping token context: {e}")

                    # Always attempt to clean up the workflow
                    await self.cleanup()
                except Exception as cleanup_error:
                    # Log but don't fail if cleanup fails
                    self._logger.error(
                        f"Error cleaning up workflow {self.name} (ID: {self._run_id}): {str(cleanup_error)}"
                    )

        self._run_task = asyncio.create_task(_execute_workflow())

        # Register this workflow with the registry
        if self.context and self.context.workflow_registry:
            await self.context.workflow_registry.register(
                workflow=self,
                run_id=self._run_id,
                workflow_id=self.id,
                task=self._run_task,
            )

        return WorkflowExecution(
            run_id=self._run_id,
            workflow_id=self._workflow_id,
        )

    async def resume(
        self, signal_name: str | None = "resume", payload: str | None = None
    ) -> bool:
        """
        Send a resume signal to the workflow.

        Args:
            signal_name: The name of the signal to send (default: "resume")
            payload: Optional data to provide to the workflow upon resuming

        Returns:
            bool: True if the resume signal was sent successfully, False otherwise
        """
        if not self._run_id:
            self._logger.error("Cannot resume workflow with no ID")
            return False

        try:
            self._logger.info(
                f"About to send {signal_name} signal sent to workflow {self._run_id}"
            )
            signal = Signal(
                name=signal_name,
                workflow_id=self.id,
                run_id=self._run_id,
                payload=payload,
            )
            await self.executor.signal_bus.signal(signal)
            self._logger.info(f"{signal_name} signal sent to workflow {self._run_id}")
            self.update_status("running")
            return True
        except Exception as e:
            self._logger.error(
                f"Error sending resume signal to workflow {self._run_id}: {e}"
            )
            return False

    async def cancel(self) -> bool:
        """
        Cancel the workflow by sending a cancel signal and cancelling its task.

        Returns:
            bool: True if the workflow was cancelled successfully, False otherwise
        """
        if not self._run_id:
            self._logger.error("Cannot cancel workflow with no ID")
            return False

        try:
            # First signal the workflow to cancel - this allows for graceful cancellation
            # when the workflow checks for cancellation
            self._logger.info(f"Sending cancel signal to workflow {self._run_id}")
            await self.executor.signal(
                "cancel", workflow_id=self.id, run_id=self._run_id
            )
            return True
        except Exception as e:
            self._logger.error(f"Error cancelling workflow {self._run_id}: {e}")
            return False

    # Add the dynamic signal handler method in the case that the workflow is running under Temporal
    if "temporalio.workflow" in sys.modules:
        from temporalio import workflow
        from temporalio.common import RawValue
        from typing import Sequence

        @workflow.signal(dynamic=True)
        async def _signal_receiver(self, name: str, args: Sequence[RawValue]):
            """Dynamic signal handler for Temporal workflows."""
            from temporalio import workflow

            self._logger.debug(f"Dynamic signal received: name={name}, args={args}")

            # Extract payload and update mailbox
            payload = args[0] if args else None

            if hasattr(self, "_signal_mailbox"):
                self._signal_mailbox.push(name, payload)
                self._logger.debug(f"Updated mailbox for signal {name}")
            else:
                self._logger.warning("No _signal_mailbox found on workflow instance")

            if hasattr(self, "_handlers"):
                # Create a signal object for callbacks
                sig_obj = Signal(
                    name=name,
                    payload=payload,
                    workflow_id=workflow.info().workflow_id,
                    run_id=workflow.info().run_id,
                )

                # Live lookup of handlers (enables callbacks added after attach_to_workflow)
                for _, cb in self._handlers.get(name, ()):
                    if asyncio.iscoroutinefunction(cb):
                        await cb(sig_obj)
                    else:
                        cb(sig_obj)

    async def get_status(self) -> Dict[str, Any]:
        """
        Get the current status of the workflow.

        Returns:
            Dict[str, Any]: A dictionary with workflow status information
        """
        status = {
            "id": self._run_id,
            "name": self.name,
            "status": self.state.status,
            "running": self._run_task is not None and not self._run_task.done()
            if self._run_task
            else False,
            "state": self.state.model_dump()
            if hasattr(self.state, "model_dump")
            else self.state.__dict__,
        }

        # Add result/error information if the task is done
        if self._run_task and self._run_task.done():
            try:
                result = self._run_task.result()

                # Convert result to a useful format
                if hasattr(result, "model_dump"):
                    result_data = result.model_dump()
                elif hasattr(result, "__dict__"):
                    result_data = result.__dict__
                else:
                    result_data = str(result)

                status["result"] = result_data
                status["completed"] = True
                status["error"] = None
            except Exception as e:
                status["result"] = None
                status["completed"] = False
                status["error"] = str(e)
                status["exception_type"] = type(e).__name__

        return status

    def update_status(self, status: str) -> None:
        """
        Update the workflow status.

        Args:
            status: The new status to set
        """
        self.state.status = status
        self.state.updated_at = datetime.now(timezone.utc).timestamp()

    # Static registry methods have been moved to the WorkflowRegistry class

    async def update_state(self, **kwargs):
        """Syntactic sugar to update workflow state."""
        for key, value in kwargs.items():
            if hasattr(self.state, "__getitem__"):
                self.state[key] = value
            setattr(self.state, key, value)

        self.state.updated_at = datetime.now(timezone.utc).timestamp()

    async def initialize(self):
        """
        Initialization method that will be called before run.
        Override this to set up any resources needed by the workflow.

        This checks the _initialized flag to prevent double initialization.
        """
        if self._initialized:
            self._logger.debug(f"Workflow {self.name} already initialized, skipping")
            return

        self.state.status = "initializing"
        self._logger.debug(f"Initializing workflow {self.name}")

        if self.context.config.execution_engine == "temporal":
            if isinstance(self.executor.signal_bus, TemporalSignalHandler):
                # Attach the signal handler to the workflow
                self.executor.signal_bus.attach_to_workflow(self)
            else:
                self._logger.warning(
                    "Signal handler not attached: executor.signal_bus is not a TemporalSignalHandler"
                )

        self._initialized = True
        self.state.updated_at = datetime.now(timezone.utc).timestamp()

    async def cleanup(self):
        """
        Cleanup method that will be called after run.
        Override this to clean up any resources used by the workflow.

        This checks the _initialized flag to ensure cleanup is only done on initialized workflows.
        """
        if not self._initialized:
            self._logger.debug(
                f"Workflow {self.name} not initialized, skipping cleanup"
            )
            return

        self._logger.debug(f"Cleaning up workflow {self.name}")
        self._initialized = False

    async def __aenter__(self):
        """Support for async context manager pattern."""
        await self.initialize()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Support for async context manager pattern."""
        await self.cleanup()
