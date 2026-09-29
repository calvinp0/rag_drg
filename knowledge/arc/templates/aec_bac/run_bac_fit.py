#!/usr/bin/env python3
"""BAC step 3: fit Petersson- and Melius-type BACs for LEVEL from the H298 values fit_bac.py stored.

Writes pbac and mbac entries into your RMG-database quantum_corrections/data.py (write_to_database,
overwrite). Set crossval_n_folds (1 = no cross-validation, -1 = leave-one-out) to check
transferability first; nothing is written to the database while cross-validating.

Run with rmg_env:   python run_bac_fit.py
"""

from arkane.encorr.bac import BACJob

from aec_bac_common import arkane_level_of_theory

KWARGS = {"weighted": False, "write_to_database": True, "overwrite": True}


def main():
    lot = arkane_level_of_theory()
    # output_directory keeps a copy of the fit next to the run (output.py, appended, and
    # <jobnum>_<level>.csv), independent of the database checkout. Distinct jobnums keep both CSVs.
    BACJob(level_of_theory=lot, bac_type="p", **KWARGS).execute(output_directory=".", jobnum=1)
    # global_opt_iter=10 is the RMG-Py default; its BAC example recommends at least 10.
    BACJob(level_of_theory=lot, bac_type="m", fit_mol_corr=True, global_opt=True, global_opt_iter=10,
           **KWARGS).execute(output_directory=".", jobnum=2)
    # A failed database write is only logged as a warning ("Could not write BACs to database").
    print("Check the log for 'Could not write BACs to database', then commit data.py in your RMG-database.")


if __name__ == "__main__":
    main()
