# pycytominer-transform

```mermaid
    flowchart LR

        images[(Images)]
        CellProfiler
        csv[(CSV)]
        DeepProfiler
        npz[(NPZ)]
        cytominer-database
        sqlite[(SQLite)]
        cp_sqlite[(SQLite)]
        pycytominer
        pycytominer-transform
        parquet[(Parquet)]

        images --> CellProfiler
        images --> DeepProfiler
        CellProfiler --> csv
        CellProfiler --> cp_sqlite
        DeepProfiler --> npz
        csv --> cytominer-database
        cytominer-database --> sqlite
        csv --> pycytominer-transform
        npz --> pycytominer-transform
        sqlite --> pycytominer-transform
        cp_sqlite --> pycytominer-transform
        pycytominer-transform --> parquet
        parquet --> pycytominer
```

_Diagram showing data flow relative to this project._

## Summary

pycytominer-transform takes input data from CSV's, SQLite, or NPZ, for conversion to [Pycytominer](https://github.com/cytomining/pycytominer) relevant output in Parquet format.
The Parquet files will have a unified and documented data model, including referenceable schema where appropriate (for validation within Pycytominer or other projects).

## Installation

Install pycytominer-transform with the following command:

```shell
pip install git+https://github.com/cytomining/pycytominer-transform.git
```

## Contributing, Development, and Testing

Please see [contributing.md](contributing.md) for more details on contributions, development, and testing.

## References

- [pycytominer](https://github.com/cytomining/pycytominer)
- [cytominer-database](https://github.com/cytomining/cytominer-database)
- [DeepProfiler](https://github.com/cytomining/DeepProfiler)
- [CellProfiler](https://github.com/CellProfiler/CellProfiler)
- [cytominer-eval](https://github.com/cytomining/cytominer-eval)
