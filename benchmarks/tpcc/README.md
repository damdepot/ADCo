# py-tpcc-python3

A Python 3 compatible TPC-C benchmark implementation, forked from the original [`apavlo/py-tpcc`](https://github.com/apavlo/py-tpcc).

## Features

- Python 3 compatibility
- Multiple database drivers: MySQL, SQLite, MongoDB, Cassandra, Redis, and more
- Original TPC-C benchmark workload (New Order, Payment, Order Status, Delivery, Stock Level)
- Local multiprocessing execution (via `tpcc.py`)
- Distributed execution across SSH nodes via execnet (via `coordinator.py` + `worker.py`)
- Data loading and benchmark execution
- Configurable scale factor and warehouse count

## Quick Start

Assuming that you already have MySQL installed on your local machine:

**Step 1:** Generate a default configuration file:

```
python tpcc.py --print-config mysql > mysql.config
```

**Step 2:** Load data and run the benchmark:

```
python tpcc.py --config mysql.config --warehouses 4 --duration 60 mysql
```

Run without loading data:

```
python tpcc.py --no-load --config mysql.config --warehouses 4 --duration 60 mysql
```

Run with multiple client processes:

```
python tpcc.py --clients 4 --config mysql.config --warehouses 4 --duration 60 mysql
```

Use the CSV driver to inspect the data:

```
python tpcc.py csv
```

## Distributed Execution

The `coordinator.py` script supports distributed execution across multiple SSH nodes using execnet:

```
python coordinator.py --config configs/CONFIG_EXAMPLE --clientprocs 5 hypertable
```

Configure client nodes and code path in the config file under the `clients` and `path` keys.

## Usage

```
usage: tpcc.py [-h] [--config CONFIG] [--reset] [--scalefactor SF]
               [--warehouses W] [--duration D] [--ddl DDL] [--clients N]
               [--stop-on-error] [--no-load] [--no-execute] [--print-config]
               [--debug]
               system

python tpcc.py mysql --config mysql.config --warehouses 4 --duration 60
```

## Project Structure

```
py-tpcc-python3/
├── __init__.py          # Package exports (createDriverClass, startLoading)
├── tpcc.py              # Local benchmark runner (multiprocessing)
├── coordinator.py       # Distributed benchmark coordinator (execnet/SSH)
├── worker.py            # Remote worker for distributed execution
├── message.py           # Message protocol for distributed mode
├── constants.py         # TPC-C constants
├── configs/             # Configuration file examples
│   └── CONFIG_EXAMPLE
├── drivers/             # Database backend drivers
│   ├── abstractdriver.py
│   ├── mysqldriver.py
│   ├── sqlitedriver.py
│   └── ... (cassandra, couchdb, csv, hbase, membase, mongodb, redis, scalaris, tokyocabinet)
├── runtime/             # Benchmark execution engine
│   ├── executor.py
│   └── loader.py
└── util/                # Utilities
    ├── rand.py, nurand.py, results.py, scaleparameters.py
```

## Credits

Based on the original `py-tpcc` by Andy Pavlo and contributors.
