import logging

logging.basicConfig(
    filename="app.log",
    encoding="utf-8",
    level=logging.DEBUG,
    format="%(asctime)s %(levelname)s %(message)s",
    datefmt="%m/%d/%Y %I:%M:%S %p",
)
TRACE_LEVEL_NUM = 5  # For TRACE level, use a numeric value lower than DEBUG (10).

logging.addLevelName(TRACE_LEVEL_NUM, "TRACE")


# Define a function for the TRACE level logging.
def trace(self, message, *args, **kwargs):
    self.log(TRACE_LEVEL_NUM, message, *args, **kwargs)


# Attach the `trace` function to the Logger class, allowing it to be used in logger instances.
logging.Logger.trace = trace

# Create a logger and set its level to TRACE.
logger = logging.getLogger("example")
logger.setLevel(logging.INFO)

# Add a console handler with a simple format
console_handler = logging.StreamHandler()
console_handler.setLevel(TRACE_LEVEL_NUM)
formatter = logging.Formatter("%(asctime)s - %(levelname)s - %(message)s")
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

