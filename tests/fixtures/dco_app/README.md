# DCo fixture app

This directory is a synthetic, intentionally tiny fixture for DCo (Application-Database
Co-design) tests. It is not a real workload: it exists only to provide deterministic
source files and a `[postgres]` connection config so the core test suite can exercise
code-hashing and workload-scanning utilities without depending on an external benchmark
checkout such as TPC-C.
