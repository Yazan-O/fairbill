# Fairbill bench

Ground truth is `gallery/truth.json`. Suite `truth` audits the bill as printed; suite `e2e` reads the phone photo first, then audits what it read.

| bill | suite | planted | found | matched | false flags | letter score | reader exact | seconds |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| bill_01 | e2e | 1 | 1 | 1/1 | 0 | 1.00 | yes | 565.8 |
| bill_01 | truth | 1 | 1 | 1/1 | 0 | 1.00 | - | 218.7 |
| bill_02 | e2e | 1 | 1 | 1/1 | 0 | 1.00 | yes | 166.2 |
| bill_02 | truth | 1 | 1 | 1/1 | 0 | 1.00 | - | 83.4 |
| bill_03 | e2e | 1 | 1 | 1/1 | 0 | 1.00 | yes | 194.1 |
| bill_03 | truth | 1 | 1 | 1/1 | 0 | 1.00 | - | 98.7 |
| bill_04 | e2e | 1 | 1 | 1/1 | 0 | 1.00 | yes | 242.3 |
| bill_04 | truth | 1 | 1 | 1/1 | 0 | 1.00 | - | 312.1 |
| bill_05 | e2e | 1 | 1 | 1/1 | 0 | 1.00 | yes | 177.5 |
| bill_05 | truth | 1 | 1 | 1/1 | 0 | 1.00 | - | 304.2 |
| bill_06 | e2e | 0 | 0 | 0/0 | 0 | - | yes | 476.8 |
| bill_06 | truth | 0 | 0 | 0/0 | 0 | 1.00 | - | 172.4 |

**5/5 truth and 5/5 e2e planted findings matched (10/10 overall), 0 row(s) with a false flag, reader 6/6 exact, letter score mean 1.00, 3682.6s total.**
