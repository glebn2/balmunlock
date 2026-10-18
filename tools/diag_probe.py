"""Read-only DIAG connection probe; never opens the partition file service."""
import argparse
import sys


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    import usb.core
    from diag_transport import DiagIdentityError, DiagUnavailable, open_transport
    transport = None
    try:
        transport = open_transport()
        if not transport.ping():
            print("DIAG connection opened, but the read-only ping was not confirmed.", file=sys.stderr)
            return 4
        print("DIAG connection verified.")
        return 0
    except DiagIdentityError as error:
        print(str(error), file=sys.stderr)
        return 3
    except (DiagUnavailable, usb.core.USBError, ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        return 2
    finally:
        if transport is not None:
            transport.close()


if __name__ == "__main__":
    raise SystemExit(main())
