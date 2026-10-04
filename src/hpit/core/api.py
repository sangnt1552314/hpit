"""The single entry point the CLI and UIs use to reach the backend.

Mock vs real is decided here, once, so no UI code needs `if mock:`.
"""

from hpit.core import config

if config.MOCK:
    from hpit.core.mock import (  # noqa: F401
        cancel_job,
        cleanup_commands,
        delete_cleanup,
        find_large_files,
        ignore,
        load_cached_cleanup,
        scan_cleanup,
        get_cluster_status,
        get_project_usage,
        get_projects,
        get_quotas,
        is_logged_in,
        login,
        get_disk_usage,
        get_doctor,
        get_job_details,
        get_jobs,
        list_directory,
        load_cached_scan,
        scan_directory,
        tail_job_log,
        tail_job_log_by_id,
    )
else:
    from hpit.core.accounting import (  # noqa: F401
        get_project_usage,
        get_projects,
        is_logged_in,
        login,
    )
    from hpit.core.cleanup import (  # noqa: F401
        cleanup_commands,
        delete_for_user as delete_cleanup,
        ignore,
        load_cached as load_cached_cleanup,
        scan_for_user as scan_cleanup,
    )
    from hpit.core.cluster import get_cluster_status  # noqa: F401
    from hpit.core.files import find_large_files, list_directory  # noqa: F401
    from hpit.core.quota import get_quotas  # noqa: F401
    from hpit.core.logs import tail_job_log, tail_job_log_by_id  # noqa: F401
    from hpit.core.pbs import cancel_job, get_job_details, get_jobs  # noqa: F401
    from hpit.core.storage import (  # noqa: F401
        get_disk_usage,
        load_cached_scan,
        scan_directory,
    )
    from hpit.core.system import get_doctor  # noqa: F401
