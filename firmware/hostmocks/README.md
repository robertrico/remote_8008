Host-side mock headers for compiling firmware C natively (make test-c /
coverage-c). Each header stands in for a LiteX-generated or libbase header;
the ethmac CSR accessors are backed by the model in test_udp_host.c so the
udp.c fork's behavior (SPEC S-NET-4, S-WIRE-2b) is testable without hardware.
