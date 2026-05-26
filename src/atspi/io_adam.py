"""Advantech ADAM-6060 driver — production I/O backend.

**STUB** — implementer to complete in Phase E (see docs/SPEC.md §8).

The ADAM-6060 exposes its DI/DO over Modbus TCP. The relevant
registers from the ADAM-6000 User Manual (verify against your
firmware revision):

  Read functions:
    Packed DI status:  holding register 40001 (PDU 0x0000), bit 0..5 = DI 0..5
    Packed DO status:  holding register 40002 (PDU 0x0001), bit 0..5 = DO 0..5

  Write functions:
    Per-DO coil set:   coils 00017..00022 (PDU 0x0010..0x0015), FC05/FC15

The mapping from ADAM channels to ATS signals is documented in
docs/HARDWARE.md §3.
"""
from __future__ import annotations

import logging

from .io_driver import InputSnapshot, OutputState

log = logging.getLogger("atspi.io_adam")


class IOAdamDriver:
    """ADAM-6060 driver. NOT YET IMPLEMENTED.

    Wire this up using pymodbus's ``AsyncModbusTcpClient`` and the
    register addresses noted in the module docstring. The
    ``read_inputs`` method must take a single packed-register read and
    decode each bit into the InputSnapshot fields per the
    HARDWARE.md §3 mapping.
    """

    def __init__(self, host: str, port: int = 502, unit_id: int = 1):
        self.host = host
        self.port = port
        self.unit_id = unit_id
        # TODO: instantiate pymodbus AsyncModbusTcpClient here

    async def connect(self) -> bool:
        # TODO: open the pymodbus connection. Return True on success.
        raise NotImplementedError("ADAM driver not yet implemented — see docs/SPEC.md §8 Phase E")

    async def close(self) -> None:
        # TODO
        pass

    async def read_inputs(self) -> InputSnapshot:
        # TODO: single read of the packed DI register, decode each bit
        # into InputSnapshot fields per HARDWARE.md §3 mapping:
        #   DI 0 → Load Disconnect (pulse → set position to 'transferring')
        #   DI 1 → On Normal aux
        #   DI 2 → On Emergency aux
        #   DI 3 → Normal source available
        #   DI 4 → Emergency source available
        #   DI 5 → Engine-start (sense)
        # Derive `position` from DI 0/1/2 combinations.
        raise NotImplementedError

    async def read_output_state(self) -> OutputState:
        # TODO: read packed DO state register
        raise NotImplementedError

    async def drive_outputs(self, **kwargs) -> None:
        # TODO: map abstract commands to coil writes
        raise NotImplementedError
