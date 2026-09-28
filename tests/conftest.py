import textwrap
from pathlib import Path

import pytest

from rag_drg.config import load_config


@pytest.fixture
def project(tmp_path: Path):
    """A tiny self-contained knowledge base + config in a temp dir."""
    kb = tmp_path / "knowledge"
    (kb / "ess" / "orca").mkdir(parents=True)
    (kb / "ess" / "gaussian").mkdir(parents=True)
    (kb / "hpc" / "templates").mkdir(parents=True)
    (kb / "lessons").mkdir()

    (kb / "ess" / "orca" / "orca.md").write_text(textwrap.dedent("""\
        ---
        title: ORCA essentials
        domain: ess
        software: orca
        version: ["5", "6"]
        doc_type: gotcha
        status: verified
        ---
        # ORCA essentials

        ## Memory
        `%maxcore` is MB per core, not total memory. Use about 75% of the memory per core.

        ## Transition states
        Use `! OptTS Freq` with `%geom Calc_Hess true end` for a TS optimisation.
        """))
    (kb / "ess" / "gaussian" / "g16.md").write_text(textwrap.dedent("""\
        ---
        title: Gaussian essentials
        domain: ess
        software: gaussian
        version: "16"
        ---
        # Gaussian

        ## Memory
        `%mem` is the total memory of the job, shared by all threads.

        ## Transition states
        Use `Opt=(TS,CalcFC,NoEigenTest)` for a TS optimisation.
        """))
    (kb / "hpc" / "templates" / "slurm_orca.sh").write_text(
        "#!/bin/bash\n#SBATCH --ntasks=16\n$(which orca) job.inp > job.out\n"
    )
    raw = tmp_path / "manual"
    raw.mkdir()
    (raw / "scf.rst").write_text(textwrap.dedent("""\
        SCF
        ===

        Convergence
        -----------

        Set ``d_convergence`` to tighten the density convergence of the SCF.
        """))

    (tmp_path / "rag_drg.yaml").write_text(textwrap.dedent("""\
        index_path: index/test.sqlite
        lessons_dir: knowledge/lessons
        chunk_size: 400
        chunk_overlap: 50
        sources:
          - name: curated
            type: local
            path: knowledge
            doc_type: card
            doc_type_rules:
              - {glob: "hpc/templates/**", doc_type: template}
          - name: manual
            type: local
            path: manual
            domain: ess
            software: psi4
        """))
    return load_config(tmp_path / "rag_drg.yaml")
