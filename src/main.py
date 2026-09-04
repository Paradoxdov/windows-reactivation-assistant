"""Windows Reactivation Assistant - console entry point.

Prepare the official offline Windows reactivation workflow on the Microsoft
Product Activation Portal, up to and including typing the Installation ID,
then stop. The Submit/Confirm button is located but never pressed, and no
submission endpoint is called directly, so this tool does not deliberately
submit the form or change the licensing state on this PC.
"""

import argparse
import os
import shutil
import sys
import time

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from waa import config as config_module
from waa import installation_id as iid_module
from waa import traces
from waa import windows_license
from waa.browser import Browser
from waa.errors import WaaError
from waa.logger import LOG_DIR_NAME, Logger, scrub
from waa.portal import MicrosoftActivationPortal

VERSION = "1.0"
BANNER = "WINDOWS REACTIVATION ASSISTANT %s" % VERSION
WIDTH = 62


def _line(char="-"):
    return char * WIDTH


def _field(label, value):
    return "%-22s %s" % (label + ":", value)


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog="WindowsReactivationAssistant",
        description="Prepare offline Windows reactivation on the Microsoft "
                    "Product Activation Portal (safe stop before Submit).")
    parser.add_argument("--verbose", action="store_true",
                        help="Print debug detail to the console as well.")
    parser.add_argument("--close-browser", action="store_true",
                        help="Close Edge when finished instead of leaving it open.")
    parser.add_argument("--reset-profile", action="store_true",
                        help="Delete the local BrowserProfile (signs out of the "
                             "Microsoft account) and exit.")
    parser.add_argument("--cleanup", action="store_true",
                        help="Best-effort cleanup of the automation profile, "
                             "logs, temporary files and matching shell history.")
    parser.add_argument("--keep-profile", action="store_true",
                        help="Keep the Microsoft session in BrowserProfile instead "
                             "of erasing it when the run ends.")
    parser.add_argument("--license-only", action="store_true",
                        help="Only detect Windows and the Installation ID; "
                             "do not start a browser.")
    parser.add_argument("--write-config", action="store_true",
                        help="Write a config.json with default values and exit.")
    parser.add_argument("--no-pause", action="store_true",
                        help="Do not wait for a key press before exiting.")
    return parser.parse_args(argv)


def reset_profile(logger, profile_dir):
    profile_dir = config_module.require_owned_profile_dir(profile_dir)
    if not os.path.isdir(profile_dir):
        logger.info("No browser profile to remove (%s)." % profile_dir)
        return 0
    try:
        shutil.rmtree(profile_dir)
    except OSError as exc:
        logger.error("Could not remove the browser profile: %s" % exc)
        return 1
    logger.info("Browser profile removed: %s" % profile_dir)
    logger.info("The next run will ask for the Microsoft account again.")
    return 0


def cleanup_everything(profile_dir, log_dir):
    """Best-effort removal of assistant-managed data and matching shell history."""
    cleanup_incomplete = False
    try:
        profile_in_temp = config_module.profile_uses_local_temp(profile_dir)
    except WaaError:
        profile_in_temp = False
    try:
        profile_dir = config_module.require_owned_profile_dir(profile_dir)
    except WaaError as exc:
        print(scrub("  refusing to remove unsafe browser profile: %s" % exc))
        profile_dir = None
        cleanup_incomplete = True
    else:
        # A missing directory has no ownership marker proving that shell MRU
        # entries with the same basename belong to this application.
        if not os.path.isdir(profile_dir):
            profile_dir = None
    print("Closing the automation browser and removing local data...")
    if profile_dir:
        try:
            from waa.browser import Browser as _Browser

            class _Quiet(object):
                def debug(self, message):
                    pass

                warn = info = error = step = debug

            _Browser(_Quiet(), profile_dir)._release_profile()
        except Exception as exc:  # noqa: BLE001 - cleanup must never crash
            print(scrub("  note: could not close Edge automatically (%s)" % exc))

    targets = [profile_dir] if profile_dir else []
    local_temp = config_module.local_temp_dir()
    for target in (log_dir, local_temp):
        if target == local_temp and profile_in_temp:
            print("  refusing to remove Temp because configured profile_dir uses it")
            cleanup_incomplete = True
            continue
        try:
            targets.append(config_module.validate_program_data_dir(target))
        except WaaError as exc:
            print(scrub("  refusing to remove unsafe local data path: %s" % exc))
            cleanup_incomplete = True

    removed = []
    for target in targets:
        if os.path.isdir(target):
            for attempt in range(3):
                try:
                    shutil.rmtree(target)
                    removed.append(target)
                    break
                except OSError:
                    time.sleep(1.5)
            else:
                print(scrub("  could not fully remove %s (files still in use)" % target))
                cleanup_incomplete = True
    for path in removed:
        print(scrub("  removed %s" % path))
    if profile_dir:
        try:
            removed_traces = traces.clean_shell_traces(profile_dir, strict=True)
        except Exception as exc:  # noqa: BLE001 - report incomplete cleanup
            print(scrub("  could not clean shell traces: %s" % exc))
            cleanup_incomplete = True
        else:
            for item in removed_traces:
                print(scrub("  removed shell trace: %s" % item))
    if cleanup_incomplete:
        print("Cleanup incomplete: some local data was left untouched.")
        return 2
    print("Cleanup finished: managed local data and matching shell traces were removed.")
    return 0


def purge_scratch(logger=None):
    """Remove this program's scratch directory, if nothing is using it."""
    try:
        scratch = config_module.validate_program_data_dir(
            config_module.local_temp_dir())
    except WaaError as exc:
        if logger:
            logger.warn("Refusing to remove unsafe scratch directory: %s" % exc)
        return
    if scratch and os.path.isdir(scratch):
        try:
            shutil.rmtree(scratch)
            if logger:
                logger.debug("Removed scratch directory %s" % scratch)
        except OSError as exc:
            if logger:
                logger.debug("Scratch directory still in use (%s)" % exc)


def sweep_temp_leftovers(logger=None):
    """Delete only the scratch directory owned by this program.

    PyInstaller also uses ``_MEI*`` directories, but their names do not encode
    which application owns them. They are deliberately left untouched.
    """
    purge_scratch(logger)


def erase_session(logger, profile_dir):
    """Close the automation browser and delete the signed-in profile."""
    try:
        profile_dir = config_module.require_owned_profile_dir(profile_dir)
    except WaaError as exc:
        logger.error("Profile cleanup refused: %s" % exc)
        return
    if not os.path.isdir(profile_dir):
        return
    try:
        Browser(logger, profile_dir)._release_profile()
    except Exception as exc:  # noqa: BLE001 - never fail the run on cleanup
        logger.debug("Could not close Edge before erasing the profile: %s" % exc)
    for _ in range(4):
        try:
            shutil.rmtree(profile_dir)
            purge_scratch(logger)
            wiped = traces.clean_shell_traces(profile_dir, logger)
            logger.info("Microsoft sign-in session erased (%s removed)." % profile_dir)
            if wiped:
                logger.info("Shell traces of the automation profile removed (%d)."
                            % len(wiped))
            return
        except OSError:
            time.sleep(1.5)
    logger.warn("Could not fully remove %s - close Edge and run --cleanup."
                % profile_dir)


def print_report(logger, license_info, iid, portal, safe_stop_reached):
    status = portal.status if portal else {}
    lines = [
        "",
        _line("="),
        BANNER.center(WIDTH),
        _line("="),
        _field("Windows", license_info.edition if license_info else "UNKNOWN"),
        _field("Build", "%s%s" % (license_info.build,
                                  " (%s)" % license_info.display_version
                                  if license_info.display_version else ""))
        if license_info else _field("Build", "UNKNOWN"),
        _field("Architecture", license_info.architecture if license_info else "UNKNOWN"),
        _field("Channel", license_info.channel if license_info else "UNKNOWN"),
        _field("Partial Key", "PRESENT (value withheld)"
               if license_info and license_info.partial_key else "UNKNOWN"),
        _field("Activation Status", license_info.status if license_info else "UNKNOWN"),
        _field("Installation ID", "OK" if iid else "NOT AVAILABLE"),
        _field("Microsoft Portal", "OK" if status.get("portal_opened") else "NOT REACHED"),
        _field("Authentication", "OK" if status.get("authenticated") else "NOT REACHED"),
        _field("Windows Activation Page",
               "OK" if status.get("iid_page") else "NOT REACHED"),
        _field("IID Inserted", "OK" if status.get("iid_inserted") else "NO"),
        _field("Submit Button",
               'FOUND ("%s")' % status.get("submit_label")
               if status.get("submit_found") else "NOT FOUND"),
        _line("-"),
    ]
    if safe_stop_reached:
        lines += [
            "SAFE STOP REACHED",
            "SUBMIT WAS NOT PRESSED",
            "READY FOR MANUAL SUBMISSION",
        ]
    else:
        lines.append("SAFE STOP NOT REACHED - see the messages above")
    lines.append(_line("="))
    for line in lines:
        logger.info(line)


def run(args, logger, settings=None):
    settings = settings or config_module.load(logger)
    if args.verbose:
        settings.verbose = True
        logger.verbose = True
    if args.close_browser:
        settings.keep_browser_open = False

    logger.info(_line("="))
    logger.info(BANNER.center(WIDTH))
    logger.info(_line("="))
    logger.info("Program directory: %s" % config_module.program_dir())
    logger.info("Log file: %s" % logger.session_log)
    logger.info("")

    if args.reset_profile:
        return reset_profile(logger, settings.profile_dir), None, None, None, False

    # --- 1. Windows + license (read-only) --------------------------------
    logger.step("Reading Windows and license information (read-only)")
    license_info = windows_license.detect(logger)
    for label, value in license_info.summary_lines():
        logger.info("  " + _field(label, value))
    logger.info("  " + _field("Product family", license_info.product_family))
    if not license_info.is_activated:
        logger.warn("This Windows installation is not currently activated (%s)."
                    % license_info.status)

    # --- 2. Installation ID (read-only) ----------------------------------
    logger.step("Requesting the Offline Installation ID from Windows")
    iid = iid_module.obtain(logger)

    if args.license_only:
        logger.info("")
        logger.info("--license-only: stopping before the browser step.")
        return 0, license_info, iid, None, False

    # --- 3. Browser + portal ---------------------------------------------
    logger.step("Starting Microsoft Edge with the local automation profile")
    browser = Browser(logger, settings.profile_dir,
                      edge_path=settings.edge_path or None,
                      keep_open=settings.keep_browser_open)
    portal = None
    try:
        browser.start("about:blank")
        logger.info("  " + _field("Edge", browser.executable))
        logger.info("  " + _field("Profile", settings.profile_dir))
        portal = MicrosoftActivationPortal(browser, logger, settings)
        portal.run_until_safe_stop(iid, windows_version=license_info.product_family)
        return 0, license_info, iid, portal, True
    finally:
        try:
            browser.stop()
        except Exception:
            logger.debug("Browser shutdown raised; ignoring.")


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    program_dir = config_module.program_dir()

    if args.write_config:
        path = config_module.write_default(
            os.path.join(program_dir, config_module.CONFIG_NAME))
        print(scrub("Wrote default configuration to %s" % path))
        return 0

    if args.cleanup:
        settings = config_module.load(None)
        return cleanup_everything(settings.profile_dir,
                                  os.path.join(program_dir, LOG_DIR_NAME))

    logger = Logger(program_dir, verbose=args.verbose)
    logger.prune_old_logs()
    exit_code = 1
    license_info = iid = portal = None
    settings = None
    safe_stop = False
    try:
        settings = config_module.load(logger)
        # Validate before purging Temp: an older configuration may have used
        # Temp itself (or a child) as profile_dir and that data must be kept.
        config_module.validate_profile_dir(settings.profile_dir)
        sweep_temp_leftovers(logger)
        exit_code, license_info, iid, portal, safe_stop = run(
            args, logger, settings=settings)
    except WaaError as exc:
        logger.exception("Workflow failed with %s" % exc.code)
        logger.info("")
        logger.error("%s" % exc.code)
        logger.info("  %s" % exc)
        if exc.hint:
            logger.info("  Hint: %s" % exc.hint)
        logger.info("  Details for support: %s" % logger.debug_log)
        exit_code = 2
    except KeyboardInterrupt:
        logger.info("")
        logger.warn("Cancelled by the operator.")
        exit_code = 130
    except Exception as exc:  # noqa: BLE001 - last-resort guard
        logger.exception("Unexpected failure")
        logger.info("")
        logger.error("UNEXPECTED_ERROR")
        logger.info("  %s: %s" % (type(exc).__name__, exc))
        logger.info("  A full stack trace was written to %s" % logger.debug_log)
        exit_code = 3

    if license_info is not None and not args.reset_profile:
        print_report(logger, license_info, iid, portal, safe_stop)
        if safe_stop:
            logger.info("")
            logger.info("The Edge window stays open so you can check the page.")
            logger.info("The assistant did not press Submit or call a submission "
                        "endpoint, and it did not change the licence on this PC.")

    wipe = bool(settings and settings.clear_profile_on_exit
                and not args.keep_profile and not args.reset_profile)
    interactive = bool(not args.no_pause and sys.stdin and sys.stdin.isatty())

    if wipe and interactive:
        logger.info("")
        logger.info("When you are done with the browser, press Enter: the "
                    "Microsoft sign-in session will be erased from this stick.")
        try:
            input("\nPress Enter to erase the session and close this window...")
        except (EOFError, KeyboardInterrupt):
            pass
        erase_session(logger, settings.profile_dir)
    elif interactive:
        try:
            input("\nPress Enter to close this window...")
        except (EOFError, KeyboardInterrupt):
            pass
    elif wipe:
        logger.warn("Browser profile kept: this run is non-interactive. "
                    "Run with --cleanup to erase the Microsoft session.")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
