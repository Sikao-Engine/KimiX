try:
    import uvloop
    uvloop.install()
except ImportError:
    pass

from kimix.cli_impl.main import cli
if __name__ == "__main__":
    cli()
