"""Request limits the service refuses a run at."""

from tests.support.service_support import (
    LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT,
    tool_names_with_the_last_of_length,
)

TUNED_AT_THE_LIMIT = dict.fromkeys(
    tool_names_with_the_last_of_length(LAST_TOOL_NAME_LENGTH_AT_THE_LIMIT), "read_only"
)
QUOTATION_MARKS_255 = '"' * 255
QUOTATION_MARKS_256 = '"' * 256
