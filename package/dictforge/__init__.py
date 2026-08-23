"""dictforge -- a validation-driven zstd dictionary trainer.

See train.py for the trainer implementation and patch_repcodes.py for the
repeat-offset patcher it uses internally. Output is a 100%-standard-format
zstd dictionary usable by any stock zstd (CLI, library, any language
binding) -- dictforge is only a *training-time* tool.
"""

__version__ = "0.1.0"
