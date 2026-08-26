# VERIFIED pktmon behaviour (tested on this machine, Windows build 26200)

## Exact working command sequence
    pktmon stop                                  # idempotent reset
    pktmon filter remove
    pktmon filter add CBVerify -i <dest_ip> -t TCP
    pktmon start --capture --comp nics --pkt-size 128 --file-name <tmp>.etl
    <generate traffic: TCP connect to dest_ip:443>
    pktmon stop
    pktmon etl2txt <tmp>.etl --out <tmp>.txt --verbose
    pktmon filter remove

NOTE: the flag is `--verbose`, NOT `-v 5`. `pktmon start --help` is invalid syntax,
the correct form is `pktmon start help`.

## Exact decoded output format (3-line groups)
    [12]0004.0514::... [Microsoft-Windows-PktMon] PktGroupId ..., Direction Tx , Type Ethernet , Component 14, ...
    \tD8-BB-C1-8F-66-CB > C8-C6-FE-DB-F9-0D, ethertype IPv4 (0x0800), length 66: (tos 0x0, ttl 128, id 46774, offset 0, flags [DF], proto TCP (6), length 52)
        192.168.1.8.14813 > 1.1.1.1.443: Flags [S], seq ..., win 65535, ...

## Parsing rules (all confirmed against real output)
1. TTL token is `ttl 128` -- lowercase, SPACE separated. NOT `ttl=`, not `TTL:`.
   Regex: r"\bttl (\d+)\b"
2. Direction lives on the PRECEDING event line: `Direction Tx ` or `Direction Rx `.
   ONLY count TTLs from packets whose event line has `Direction Tx`.
   Inbound (Rx) packets from the destination showed `ttl 58` -- counting those would
   produce a completely wrong answer.
3. pktmon logs each packet TWICE (once per component, e.g. Component 14 and 143).
   Take the MODE (most common value) of all Tx TTLs, not the first match.
4. Confirm direction a second way if desired: the 3rd line reads
   `<local_ip>.<port> > <dest_ip>.443` for outbound.

## Result of the live test
Configured `Default Hop Limit : 128 hops`, observed egress `ttl 128` on all Tx packets.
Verification path is sound.
