## apim-1

### apim-basic-mock · agent: max 10500 RPM (then {'rpm': 11280, 'reason': 'errors'})  top step 12000

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1200/1200 |  | 12,668 | 12,674 | 12,683 | 9 | 12 | - | {'mock-a': 1200} |
| 1,500 | pass | 3000/3000 |  | 12,666 | 12,674 | 12,684 | 7 | 12 | - | {'mock-a': 3000} |
| 3,000 | pass | 6000/6000 |  | 12,665 | 12,673 | 12,684 | 7 | 11 | - | {'mock-a': 6000} |
| 6,000 | pass | 12000/12000 |  | 12,665 | 12,681 | 12,748 | 6 | 18 | - | {'mock-a': 12000} |
| 9,000 | pass | 17981/18000 | {'5xx': 19} | 12,665 | 12,751 | 13,085 | 6 | 89 | - | {'mock-a': 17981} |
| 10,500 | pass | 20883/21000 | {'5xx': 117} | 12,666 | 12,899 | 13,330 | 7 | 237 | - | {'mock-a': 20883} |
| 11,280 | errors | 22273/22560 | {'5xx': 287} | 12,670 | 13,221 | 13,838 | 11 | 559 | - | {'mock-a': 22273} |
| 12,000 | errors | 23602/24000 | {'5xx': 398} | 12,669 | 12,960 | 13,170 | 10 | 298 | - | {'mock-a': 23602} |

### apim-basic-mock · chat: max 4500 RPM (then {'rpm': 5280, 'reason': 'latency'})  top step 6000

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1199/1200 | {'5xx': 1} | 9,528 | 9,571 | 9,614 | 8 | 51 | 1,256 | {'mock-a': 757, 'mock-b': 442} |
| 1,500 | pass | 3000/3000 |  | 9,694 | 10,642 | 11,496 | 175 | 1,122 | 2,306 | {'mock-a': 3000} |
| 3,000 | pass | 6000/6000 |  | 10,591 | 11,574 | 11,971 | 1,072 | 2,054 | 3,218 | {'mock-a': 6000} |
| 4,500 | pass | 9000/9000 |  | 10,835 | 11,793 | 12,260 | 1,316 | 2,272 | 3,383 | {'mock-a': 9000} |
| 5,280 | latency | 10560/10560 |  | 11,237 | 13,686 | 16,338 | 1,717 | 4,166 | 5,133 | {'mock-a': 10560} |
| 6,000 | latency | 12000/12000 |  | 11,700 | 14,052 | 14,923 | 2,180 | 4,532 | 5,445 | {'mock-a': 12000} |

### apim-basic-mock · rag: max 2640 RPM (then {'rpm': 3000, 'reason': 'latency'})  top step 3000

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1199/1200 | {'5xx': 1} | 6,794 | 6,810 | 6,821 | 7 | 20 | 1,826 | {'mock-a': 714, 'mock-b': 485} |
| 1,500 | pass | 3000/3000 |  | 6,802 | 6,887 | 6,946 | 14 | 96 | 1,903 | {'mock-a': 3000} |
| 2,280 | pass | 4560/4560 |  | 6,912 | 7,302 | 7,725 | 124 | 511 | 2,301 | {'mock-a': 4560} |
| 2,640 | pass | 5280/5280 |  | 7,280 | 8,238 | 8,657 | 493 | 1,448 | 3,234 | {'mock-a': 5280} |
| 3,000 | latency | 6000/6000 |  | 7,564 | 8,536 | 9,094 | 776 | 1,746 | 3,511 | {'mock-a': 6000} |

### apim-basic-mock · short: max ≥ 22500 RPM (then None)  top step 22500

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1200/1200 |  | 2,710 | 2,714 | 2,721 | 6 | 9 | - | {'mock-a': 1200} |
| 1,500 | pass | 3000/3000 |  | 2,709 | 2,714 | 2,722 | 6 | 9 | - | {'mock-a': 3000} |
| 3,000 | pass | 6000/6000 |  | 2,709 | 2,714 | 2,722 | 6 | 8 | - | {'mock-a': 6000} |
| 6,000 | pass | 12000/12000 |  | 2,708 | 2,714 | 2,724 | 5 | 9 | - | {'mock-a': 12000} |
| 9,000 | pass | 18000/18000 |  | 2,708 | 2,718 | 2,740 | 4 | 13 | - | {'mock-a': 18000} |
| 12,000 | pass | 24000/24000 |  | 2,708 | 2,723 | 2,764 | 4 | 18 | - | {'mock-a': 24000} |
| 15,000 | pass | 30000/30000 |  | 2,708 | 2,729 | 2,798 | 4 | 23 | - | {'mock-a': 30000} |
| 22,500 | pass | 45000/45000 |  | 2,708 | 2,764 | 2,930 | 4 | 59 | - | {'mock-a': 45000} |

### apim-std-mock · agent: max 16860 RPM (then {'rpm': 18720, 'reason': 'errors'})  top step 22500

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1200/1200 |  | 12,668 | 12,676 | 12,692 | 9 | 14 | - | {'mock-a': 1200} |
| 1,500 | pass | 3000/3000 |  | 12,667 | 12,675 | 12,684 | 8 | 13 | - | {'mock-a': 3000} |
| 3,000 | pass | 6000/6000 |  | 12,665 | 12,674 | 12,685 | 6 | 12 | - | {'mock-a': 6000} |
| 6,000 | pass | 12000/12000 |  | 12,665 | 12,674 | 12,688 | 6 | 12 | - | {'mock-a': 12000} |
| 9,000 | pass | 18000/18000 |  | 12,665 | 12,677 | 12,722 | 6 | 15 | - | {'mock-a': 18000} |
| 12,000 | pass | 24000/24000 |  | 12,665 | 12,683 | 12,731 | 6 | 21 | - | {'mock-a': 24000} |
| 15,000 | pass | 30000/30000 |  | 12,665 | 12,687 | 12,770 | 6 | 25 | - | {'mock-a': 30000} |
| 16,860 | pass | 33720/33720 |  | 12,667 | 12,808 | 13,565 | 8 | 146 | - | {'mock-a': 33720} |
| 18,720 | errors | 28012/37440 | {'5xx': 9426, 'timeout': 2} | 12,731 | 15,199 | 18,335 | 72 | 2,537 | - | {'mock-a': 25008, 'mock-b': 2958, 'mock-c': 46} |
| 22,500 | errors | 40407/45000 | {'5xx': 4593} | 12,670 | 13,116 | 14,402 | 12 | 454 | - | {'mock-a': 37387, 'mock-b': 2997, 'mock-c': 23} |

### apim-std-mock · chat: max 12000 RPM (then {'rpm': 12720, 'reason': 'latency'})  top step 15000

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1200/1200 |  | 9,525 | 9,533 | 9,549 | 5 | 12 | 1,216 | {'mock-a': 1200} |
| 1,500 | pass | 3000/3000 |  | 9,526 | 9,562 | 9,640 | 6 | 42 | 1,247 | {'mock-a': 3000} |
| 3,000 | pass | 6000/6000 |  | 9,611 | 10,041 | 10,447 | 91 | 521 | 1,720 | {'mock-a': 6000} |
| 6,000 | pass | 12000/12000 |  | 10,024 | 11,164 | 11,678 | 504 | 1,644 | 2,819 | {'mock-a': 12000} |
| 9,000 | pass | 18000/18000 |  | 10,367 | 11,220 | 11,602 | 847 | 1,700 | 2,832 | {'mock-a': 18000} |
| 12,000 | pass | 24000/24000 |  | 10,528 | 11,579 | 12,241 | 1,008 | 2,059 | 3,169 | {'mock-a': 24000} |
| 12,720 | latency | 25440/25440 |  | 10,677 | 12,770 | 14,158 | 1,158 | 3,250 | 4,331 | {'mock-a': 25440} |
| 13,500 | latency | 27000/27000 |  | 10,868 | 13,295 | 14,456 | 1,349 | 3,775 | 4,671 | {'mock-a': 27000} |
| 15,000 | latency | 30000/30000 |  | 10,830 | 13,482 | 15,727 | 1,310 | 3,962 | 5,033 | {'mock-a': 30000} |

### apim-std-mock · rag: max 5280 RPM (then {'rpm': 6000, 'reason': 'latency'})  top step 6000

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1200/1200 |  | 6,793 | 6,801 | 6,810 | 6 | 10 | 1,818 | {'mock-a': 1200} |
| 1,500 | pass | 2999/3000 | {'5xx': 1} | 6,794 | 6,814 | 6,854 | 6 | 23 | 1,829 | {'mock-a': 2253, 'mock-b': 746} |
| 3,000 | pass | 6000/6000 |  | 6,798 | 6,848 | 6,896 | 10 | 57 | 1,863 | {'mock-a': 6000} |
| 4,500 | pass | 9000/9000 |  | 6,870 | 7,533 | 7,932 | 82 | 743 | 2,538 | {'mock-a': 9000} |
| 5,280 | pass | 10560/10560 |  | 6,936 | 8,347 | 8,949 | 148 | 1,556 | 3,350 | {'mock-a': 10560} |
| 6,000 | latency | 12000/12000 |  | 7,110 | 8,674 | 9,095 | 322 | 1,884 | 3,654 | {'mock-a': 12000} |

### apim-std-mock · short: max ≥ 22500 RPM (then None)  top step 22500

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1200/1200 |  | 2,709 | 2,718 | 2,728 | 6 | 13 | - | {'mock-a': 1200} |
| 1,500 | pass | 3000/3000 |  | 2,709 | 2,716 | 2,723 | 6 | 10 | - | {'mock-a': 3000} |
| 3,000 | pass | 6000/6000 |  | 2,709 | 2,715 | 2,725 | 5 | 10 | - | {'mock-a': 6000} |
| 6,000 | pass | 12000/12000 |  | 2,708 | 2,716 | 2,727 | 5 | 11 | - | {'mock-a': 12000} |
| 9,000 | pass | 18000/18000 |  | 2,708 | 2,716 | 2,726 | 5 | 10 | - | {'mock-a': 18000} |
| 12,000 | pass | 24000/24000 |  | 2,708 | 2,716 | 2,729 | 5 | 11 | - | {'mock-a': 24000} |
| 15,000 | pass | 30000/30000 |  | 2,708 | 2,717 | 2,732 | 4 | 12 | - | {'mock-a': 30000} |
| 22,500 | pass | 45000/45000 |  | 2,708 | 2,722 | 2,747 | 4 | 17 | - | {'mock-a': 45000} |

### failover apim-basic-perf-mock at 1200 RPM

| phase | requests | error rate | errors | p50 | p95 | moved after | members |
|---|---|---|---|---|---|---|---|
| healthy | 1200 | 0.00% |  | 9,549 | 9,635 | - | {'mock-a': 1188, 'mock-b': 12} |
| mock-a 429 | 2400 | 0.00% |  | 9,546 | 9,645 | 0 s | {'mock-b': 2400} |
| mock-a 200 | 2400 | 0.04% | {'500': 1} | 9,537 | 9,632 | 0 s | {'mock-a': 2381, 'mock-b': 18} |
| mock-a 500 | 2400 | 0.08% | {'500': 2} | 9,537 | 9,630 | 0 s | {'mock-b': 2398} |
| mock-a 200 | 2400 | 0.62% | {'500': 15} | 9,538 | 9,635 | 60 s | {'mock-a': 1200, 'mock-b': 1185} |
| mock-a hang | 2400 | 99.33% | {'500': 2384} | 9,544 | 9,587 | - | {'mock-b': 14, 'mock-a': 2} |
| mock-a 200 | 2400 | 0.00% |  | 9,552 | 9,734 | - | {'mock-b': 2387, 'mock-a': 13} |

### failover apim-std-perf-mock at 1200 RPM

| phase | requests | error rate | errors | p50 | p95 | moved after | members |
|---|---|---|---|---|---|---|---|
| healthy | 1200 | 0.00% |  | 9,527 | 9,558 | - | {'mock-a': 1198, 'mock-b': 2} |
| mock-a 429 | 2400 | 0.00% |  | 9,527 | 9,544 | 0 s | {'mock-b': 2399, 'mock-a': 1} |
| mock-a 200 | 2400 | 0.08% | {'500': 2} | 9,526 | 9,548 | 1 s | {'mock-a': 2375, 'mock-b': 23} |
| mock-a 500 | 2400 | 0.08% | {'500': 2} | 9,527 | 9,547 | 0 s | {'mock-b': 2397, 'mock-a': 1} |
| mock-a 200 | 2400 | 0.12% | {'500': 3} | 9,526 | 9,544 | 61 s | {'mock-b': 1200, 'mock-a': 1197} |
| mock-a hang | 2400 | 99.83% | {'500': 2396} | 9,524 | 9,549 | 119 s | {'mock-a': 2, 'mock-b': 2} |
| mock-a 200 | 2400 | 0.04% | {'500': 1} | 9,527 | 9,551 | - | {'mock-b': 2392, 'mock-a': 7} |

## apim-real-1

### apim-basic-perf-luna · agent: max ≥ 8 RPM (then None)  top step 8

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 2 | pass | 4/4 |  | 3,656 | 4,782 | 4,782 | - | - | - | {'Central US': 4} |
| 4 | pass | 8/8 |  | 3,686 | 4,263 | 4,263 | - | - | - | {'Central US': 8} |
| 8 | pass | 16/16 |  | 4,484 | 5,872 | 5,872 | - | - | - | {'East US 2': 10, 'Central US': 6} |

### apim-basic-perf-luna · chat: max ≥ 160 RPM (then None)  top step 160

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 20 | pass | 40/40 |  | 4,933 | 6,000 | 6,161 | - | - | 1,075 | {'Central US': 40} |
| 40 | pass | 80/80 |  | 5,019 | 6,077 | 6,488 | - | - | 1,185 | {'Central US': 80} |
| 80 | pass | 160/160 |  | 4,905 | 5,807 | 6,140 | - | - | 1,244 | {'Central US': 160} |
| 160 | pass | 320/320 |  | 5,037 | 6,033 | 11,464 | - | - | 1,284 | {'Central US': 239, 'East US 2': 81} |

### apim-basic-perf-luna · short: max ≥ 360 RPM (then None)  top step 360

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 30 | pass | 60/60 |  | 1,651 | 2,283 | 2,864 | - | - | - | {'Central US': 54, 'East US 2': 6} |
| 60 | pass | 120/120 |  | 1,612 | 2,101 | 4,097 | - | - | - | {'Central US': 120} |
| 120 | pass | 240/240 |  | 1,645 | 2,393 | 2,926 | - | - | - | {'Central US': 240} |
| 240 | pass | 480/480 |  | 1,597 | 2,366 | 4,382 | - | - | - | {'Central US': 313, 'East US 2': 167} |
| 360 | pass | 720/720 |  | 1,602 | 3,433 | 5,410 | - | - | - | {'East US 2': 462, 'Central US': 258} |

### apim-std-perf-luna · agent: max ≥ 8 RPM (then None)  top step 8

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 2 | pass | 4/4 |  | 3,843 | 4,206 | 4,206 | - | - | - | {'Central US': 4} |
| 4 | pass | 8/8 |  | 4,024 | 8,234 | 8,234 | - | - | - | {'Central US': 8} |
| 8 | pass | 16/16 |  | 4,387 | 8,274 | 8,274 | - | - | - | {'East US 2': 9, 'Central US': 7} |

### apim-std-perf-luna · chat: max ≥ 160 RPM (then None)  top step 160

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 20 | pass | 40/40 |  | 5,216 | 6,106 | 6,155 | - | - | 1,194 | {'Central US': 40} |
| 40 | pass | 80/80 |  | 4,758 | 5,465 | 5,970 | - | - | 1,154 | {'Central US': 80} |
| 80 | pass | 160/160 |  | 4,988 | 5,967 | 6,341 | - | - | 1,036 | {'Central US': 160} |
| 160 | pass | 320/320 |  | 4,848 | 5,852 | 6,236 | - | - | 1,183 | {'Central US': 239, 'East US 2': 81} |

### apim-std-perf-luna · short: max ≥ 360 RPM (then None)  top step 360

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 30 | pass | 60/60 |  | 1,719 | 1,982 | 2,412 | - | - | - | {'Central US': 60} |
| 60 | pass | 120/120 |  | 1,666 | 2,488 | 5,616 | - | - | - | {'Central US': 120} |
| 120 | pass | 240/240 |  | 1,586 | 2,162 | 2,430 | - | - | - | {'Central US': 240} |
| 240 | pass | 480/480 |  | 1,605 | 2,320 | 4,894 | - | - | - | {'Central US': 333, 'East US 2': 147} |
| 360 | pass | 720/720 |  | 1,533 | 2,568 | 4,492 | - | - | - | {'East US 2': 520, 'Central US': 200} |

### direct-luna · agent: max 4 RPM (then {'rpm': 8, 'reason': 'errors'})  top step 8

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 2 | pass | 4/4 |  | 4,183 | 4,650 | 4,650 | - | - | - | {'Central US': 4} |
| 4 | pass | 8/8 |  | 3,745 | 4,247 | 4,247 | - | - | - | {'Central US': 8} |
| 8 | errors | 7/16 | {'429': 9} | 3,828 | 4,480 | 4,480 | - | - | - | {'Central US': 7} |

### direct-luna · chat: max 80 RPM (then {'rpm': 160, 'reason': 'errors'})  top step 160

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 20 | pass | 40/40 |  | 4,965 | 5,896 | 6,126 | - | - | 942 | {'Central US': 40} |
| 40 | pass | 80/80 |  | 4,576 | 5,624 | 5,700 | - | - | 1,015 | {'Central US': 80} |
| 80 | pass | 160/160 |  | 4,692 | 5,530 | 5,962 | - | - | 1,147 | {'Central US': 160} |
| 160 | errors | 241/320 | {'429': 79} | 5,064 | 5,888 | 6,128 | - | - | 1,059 | {'Central US': 241} |

### direct-luna · short: max 120 RPM (then {'rpm': 240, 'reason': 'errors'})  top step 360

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 30 | pass | 60/60 |  | 1,663 | 2,174 | 2,516 | - | - | - | {'Central US': 60} |
| 60 | pass | 120/120 |  | 1,617 | 2,110 | 2,571 | - | - | - | {'Central US': 120} |
| 120 | pass | 240/240 |  | 1,598 | 2,251 | 3,406 | - | - | - | {'Central US': 240} |
| 240 | errors | 336/480 | {'429': 144} | 1,594 | 2,207 | 3,039 | - | - | - | {'Central US': 336} |
| 360 | errors | 250/720 | {'429': 470} | 1,639 | 2,237 | 2,634 | - | - | - | {'Central US': 250} |

## aca-1

### aca-mock · agent: max ≥ 22500 RPM (then None)  top step 22500

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1200/1200 |  | 12,684 | 12,708 | 12,847 | 27 | 48 | - | {'perf-mock--mock-a': 1200} |
| 1,500 | pass | 3000/3000 |  | 12,683 | 12,707 | 12,828 | 25 | 47 | - | {'perf-mock--mock-a': 3000} |
| 3,000 | pass | 6000/6000 |  | 12,680 | 12,701 | 12,854 | 22 | 41 | - | {'perf-mock--mock-a': 6000} |
| 6,000 | pass | 12000/12000 |  | 12,679 | 12,699 | 12,942 | 22 | 38 | - | {'perf-mock--mock-a': 12000} |
| 9,000 | pass | 18000/18000 |  | 12,679 | 12,705 | 12,932 | 22 | 44 | - | {'perf-mock--mock-a': 18000} |
| 12,000 | pass | 23986/24000 | {'5xx': 14} | 12,680 | 12,714 | 14,624 | 22 | 54 | - | {'perf-mock--mock-a': 23986} |
| 15,000 | pass | 29995/30000 | {'5xx': 5} | 12,680 | 12,722 | 16,652 | 22 | 62 | - | {'perf-mock--mock-a': 29995} |
| 22,500 | pass | 44999/45000 | {'5xx': 1} | 12,682 | 12,750 | 13,476 | 24 | 89 | - | {'perf-mock--mock-a': 44999} |

### aca-mock · chat: max 3720 RPM (then {'rpm': 4500, 'reason': 'errors'})  top step 6000

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1200/1200 |  | 9,589 | 9,720 | 10,438 | 69 | 195 | 1,269 | {'perf-mock--mock-a': 1200} |
| 1,500 | pass | 3000/3000 |  | 9,604 | 10,173 | 13,750 | 84 | 648 | 1,374 | {'perf-mock--mock-a': 3000} |
| 3,000 | pass | 6000/6000 |  | 9,654 | 10,868 | 11,977 | 135 | 1,343 | 2,109 | {'perf-mock--mock-a': 6000} |
| 3,720 | pass | 7440/7440 |  | 9,745 | 11,857 | 13,641 | 226 | 2,333 | 2,800 | {'perf-mock--mock-a': 7440} |
| 4,500 | errors | 8884/9000 | {'5xx': 103, 'connection': 13} | 31,676 | 78,158 | 100,892 | 22,157 | 68,634 | 66,053 | {'perf-mock--mock-a': 8884} |
| 6,000 | errors | 11764/12000 | {'5xx': 91, 'connection': 145} | 79,714 | 176,868 | 209,360 | 70,195 | 167,344 | 161,240 | {'perf-mock--mock-a': 11764} |

### aca-mock · rag: max 5280 RPM (then {'rpm': 6000, 'reason': 'latency'})  top step 6000

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1200/1200 |  | 6,837 | 6,876 | 7,259 | 50 | 88 | 1,857 | {'perf-mock--mock-a': 1200} |
| 1,500 | pass | 3000/3000 |  | 6,839 | 6,951 | 10,218 | 52 | 163 | 1,877 | {'perf-mock--mock-a': 3000} |
| 3,000 | pass | 6000/6000 |  | 6,853 | 7,068 | 8,158 | 66 | 280 | 1,913 | {'perf-mock--mock-a': 6000} |
| 4,500 | pass | 9000/9000 |  | 6,880 | 7,982 | 8,771 | 93 | 1,194 | 2,614 | {'perf-mock--mock-a': 9000} |
| 5,280 | pass | 10548/10560 | {'5xx': 12} | 6,908 | 8,483 | 10,091 | 121 | 1,695 | 3,030 | {'perf-mock--mock-a': 10548} |
| 6,000 | latency | 12000/12000 |  | 6,990 | 9,039 | 10,404 | 203 | 2,251 | 3,375 | {'perf-mock--mock-a': 12000} |

### aca-mock · short: max ≥ 22500 RPM (then None)  top step 22500

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1200/1200 |  | 2,725 | 2,747 | 6,118 | 22 | 40 | - | {'perf-mock--mock-a': 1200} |
| 1,500 | pass | 3000/3000 |  | 2,723 | 2,744 | 2,824 | 20 | 37 | - | {'perf-mock--mock-a': 3000} |
| 3,000 | pass | 6000/6000 |  | 2,721 | 2,734 | 2,815 | 18 | 27 | - | {'perf-mock--mock-a': 6000} |
| 6,000 | pass | 12000/12000 |  | 2,721 | 2,735 | 2,866 | 18 | 28 | - | {'perf-mock--mock-a': 12000} |
| 9,000 | pass | 18000/18000 |  | 2,721 | 2,747 | 3,006 | 18 | 41 | - | {'perf-mock--mock-a': 18000} |
| 12,000 | pass | 24000/24000 |  | 2,721 | 2,746 | 3,105 | 18 | 40 | - | {'perf-mock--mock-a': 24000} |
| 15,000 | pass | 30000/30000 |  | 2,721 | 2,743 | 3,578 | 18 | 37 | - | {'perf-mock--mock-a': 30000} |
| 22,500 | pass | 45000/45000 |  | 2,722 | 2,778 | 9,327 | 19 | 71 | - | {'perf-mock--mock-a': 45000} |

### failover aca-perf-mock at 1200 RPM

| phase | requests | error rate | errors | p50 | p95 | moved after | members |
|---|---|---|---|---|---|---|---|
| healthy | 1200 | 0.00% |  | 9,602 | 9,734 | - | {'perf-mock--mock-a': 1192, 'perf-mock--mock-b': 8} |
| mock-a 429 | 2400 | 0.00% |  | 9,599 | 9,868 | 0 s | {'perf-mock--mock-b': 2400} |
| mock-a 200 | 2400 | 0.00% |  | 9,598 | 9,888 | - | {'perf-mock--mock-a': 2315, 'perf-mock--mock-b': 85} |
| mock-a 500 | 2400 | 0.00% |  | 9,597 | 9,962 | 0 s | {'perf-mock--mock-b': 2400} |
| mock-a 200 | 2400 | 0.00% |  | 9,597 | 10,173 | - | {'perf-mock--mock-a': 2220, 'perf-mock--mock-b': 180} |
| mock-a hang | 2400 | 0.42% | {'connection': 8, 'timeout': 2} | 129,691 | 130,883 | 0 s | {'perf-mock--mock-b': 2384, 'perf-mock--mock-a': 6} |
| mock-a 200 | 2400 | 0.12% | {'connection': 1, '503': 2} | 9,633 | 10,276 | - | {'perf-mock--mock-b': 2396, 'perf-mock--mock-a': 1} |

## aca-real-1

### aca-perf-luna · agent: max 4 RPM (then {'rpm': 8, 'reason': 'errors'})  top step 8

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 2 | pass | 4/4 |  | 4,432 | 7,577 | 7,577 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 4} |
| 4 | pass | 8/8 |  | 4,291 | 5,384 | 5,384 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 8} |
| 8 | errors | 14/16 | {'4xx': 2} | 4,987 | 6,802 | 6,802 | - | - | - | {'perf-luna--pool-control-plane-test-gpt-6-luna': 7, 'perf-luna--pool-openaiminggu-gpt-6-luna': 7} |

### aca-perf-luna · chat: max ≥ 160 RPM (then None)  top step 160

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 20 | pass | 40/40 |  | 5,137 | 5,635 | 6,726 | - | - | 1,283 | {'perf-luna--pool-openaiminggu-gpt-6-luna': 36, 'perf-luna--pool-control-plane-test-gpt-6-luna': 4} |
| 40 | pass | 80/80 |  | 5,061 | 5,684 | 6,591 | - | - | 1,460 | {'perf-luna--pool-openaiminggu-gpt-6-luna': 80} |
| 80 | pass | 160/160 |  | 5,118 | 5,922 | 6,635 | - | - | 1,497 | {'perf-luna--pool-openaiminggu-gpt-6-luna': 160} |
| 160 | pass | 320/320 |  | 5,152 | 6,832 | 8,825 | - | - | 2,491 | {'perf-luna--pool-control-plane-test-gpt-6-luna': 162, 'perf-luna--pool-openaiminggu-gpt-6-luna': 158} |

### aca-perf-luna · short: max 240 RPM (then {'rpm': 360, 'reason': 'errors'})  top step 360

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 30 | pass | 60/60 |  | 1,892 | 3,479 | 5,039 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 39, 'perf-luna--pool-control-plane-test-gpt-6-luna': 21} |
| 60 | pass | 120/120 |  | 1,676 | 2,412 | 2,593 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 120} |
| 120 | pass | 240/240 |  | 1,610 | 3,129 | 5,213 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 240} |
| 240 | pass | 480/480 |  | 1,619 | 2,906 | 5,343 | - | - | - | {'perf-luna--pool-control-plane-test-gpt-6-luna': 241, 'perf-luna--pool-openaiminggu-gpt-6-luna': 239} |
| 360 | errors | 512/720 | {'4xx': 208} | 1,584 | 2,409 | 4,691 | - | - | - | {'perf-luna--pool-control-plane-test-gpt-6-luna': 276, 'perf-luna--pool-openaiminggu-gpt-6-luna': 236} |

### direct-luna · agent: max 4 RPM (then {'rpm': 8, 'reason': 'errors'})  top step 8

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 2 | pass | 4/4 |  | 4,772 | 5,545 | 5,545 | - | - | - | {'Central US': 4} |
| 4 | pass | 8/8 |  | 4,231 | 6,465 | 6,465 | - | - | - | {'Central US': 8} |
| 8 | errors | 7/16 | {'429': 9} | 4,081 | 4,669 | 4,669 | - | - | - | {'Central US': 7} |

### direct-luna · chat: max 80 RPM (then {'rpm': 160, 'reason': 'errors'})  top step 160

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 20 | pass | 40/40 |  | 4,827 | 5,474 | 5,754 | - | - | 1,203 | {'Central US': 40} |
| 40 | pass | 80/80 |  | 4,990 | 5,944 | 6,492 | - | - | 1,068 | {'Central US': 80} |
| 80 | pass | 160/160 |  | 4,810 | 5,675 | 6,088 | - | - | 1,217 | {'Central US': 160} |
| 160 | errors | 241/320 | {'429': 79} | 4,961 | 5,727 | 6,240 | - | - | 1,133 | {'Central US': 241} |

### direct-luna · short: max 120 RPM (then {'rpm': 240, 'reason': 'errors'})  top step 360

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 30 | pass | 60/60 |  | 1,839 | 2,514 | 3,464 | - | - | - | {'Central US': 60} |
| 60 | pass | 120/120 |  | 1,702 | 2,275 | 2,810 | - | - | - | {'Central US': 120} |
| 120 | pass | 240/240 |  | 1,556 | 2,526 | 4,332 | - | - | - | {'Central US': 240} |
| 240 | errors | 336/480 | {'429': 144} | 1,577 | 2,517 | 4,158 | - | - | - | {'Central US': 336} |
| 360 | errors | 250/720 | {'429': 470} | 1,585 | 2,270 | 4,689 | - | - | - | {'Central US': 250} |

## aca-real-2

### aca-perf-luna · agent: max ≥ 8 RPM (then None)  top step 8

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 2 | pass | 4/4 |  | 3,696 | 3,992 | 3,992 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 4} |
| 4 | pass | 8/8 |  | 3,505 | 3,982 | 3,982 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 8} |
| 8 | pass | 16/16 |  | 3,790 | 4,852 | 4,852 | - | - | - | {'perf-luna--pool-control-plane-test-gpt-6-luna': 7, 'perf-luna--pool-openaiminggu-gpt-6-luna': 7, 'perf-luna--pool-ai-minggu3566ai776846365708-gpt-6-luna-dz': 2} |

### aca-perf-luna · chat: max ≥ 160 RPM (then None)  top step 160

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 20 | pass | 40/40 |  | 3,948 | 4,682 | 22,774 | - | - | 1,391 | {'perf-luna--pool-openaiminggu-gpt-6-luna': 35, 'perf-luna--pool-control-plane-test-gpt-6-luna': 5} |
| 40 | pass | 80/80 |  | 4,144 | 4,810 | 5,602 | - | - | 1,042 | {'perf-luna--pool-openaiminggu-gpt-6-luna': 80} |
| 80 | pass | 160/160 |  | 4,248 | 4,922 | 5,794 | - | - | 1,131 | {'perf-luna--pool-openaiminggu-gpt-6-luna': 160} |
| 160 | pass | 320/320 |  | 4,071 | 5,759 | 7,539 | - | - | 2,555 | {'perf-luna--pool-control-plane-test-gpt-6-luna': 161, 'perf-luna--pool-openaiminggu-gpt-6-luna': 159} |

### aca-perf-luna · short: max ≥ 360 RPM (then None)  top step 360

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 30 | pass | 60/60 |  | 1,543 | 1,828 | 2,687 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 60} |
| 60 | pass | 120/120 |  | 1,483 | 1,951 | 2,588 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 120} |
| 120 | pass | 240/240 |  | 1,387 | 1,974 | 4,394 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 240} |
| 240 | pass | 480/480 |  | 1,446 | 2,947 | 5,244 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 240, 'perf-luna--pool-control-plane-test-gpt-6-luna': 240} |
| 360 | pass | 720/720 |  | 1,370 | 1,940 | 3,176 | - | - | - | {'perf-luna--pool-control-plane-test-gpt-6-luna': 287, 'perf-luna--pool-openaiminggu-gpt-6-luna': 232, 'perf-luna--pool-ai-minggu3566ai776846365708-gpt-6-luna-dz': 138, 'perf-luna--pool-minggu-luna-eu-22610-gpt-6-luna-dz': 63} |

## aks-1

### aks-mock · agent: max ≥ 15000 RPM (then None)  top step 15000

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1200/1200 |  | 12,686 | 12,698 | 12,708 | 26 | 35 | - | {'perf-mock--mock-a': 1200} |
| 1,500 | pass | 2997/3000 | {'5xx': 3} | 12,685 | 12,695 | 12,703 | 25 | 31 | - | {'perf-mock--mock-a': 2997} |
| 3,000 | pass | 5999/6000 | {'5xx': 1} | 12,684 | 12,696 | 12,743 | 24 | 32 | - | {'perf-mock--mock-a': 5999} |
| 6,000 | pass | 12000/12000 |  | 12,683 | 12,690 | 12,697 | 23 | 27 | - | {'perf-mock--mock-a': 12000} |
| 9,000 | pass | 17999/18000 | {'5xx': 1} | 12,682 | 12,690 | 12,697 | 22 | 26 | - | {'perf-mock--mock-a': 17999} |
| 12,000 | pass | 23999/24000 | {'5xx': 1} | 12,682 | 12,690 | 12,698 | 22 | 26 | - | {'perf-mock--mock-a': 23999} |
| 15,000 | pass | 29998/30000 | {'5xx': 2} | 12,681 | 12,691 | 12,700 | 21 | 27 | - | {'perf-mock--mock-a': 29998} |

### aks-mock · chat: max 11280 RPM (then {'rpm': 12000, 'reason': 'latency'})  top step 12000

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1200/1200 |  | 9,577 | 9,591 | 9,683 | 57 | 70 | 1,232 | {'perf-mock--mock-a': 1200} |
| 1,500 | pass | 3000/3000 |  | 9,582 | 9,597 | 9,624 | 62 | 76 | 1,232 | {'perf-mock--mock-a': 3000} |
| 3,000 | pass | 6000/6000 |  | 9,595 | 9,628 | 10,014 | 75 | 108 | 1,247 | {'perf-mock--mock-a': 6000} |
| 6,000 | pass | 11993/12000 | {'5xx': 7} | 9,643 | 9,901 | 11,029 | 123 | 380 | 1,359 | {'perf-mock--mock-a': 11993} |
| 9,000 | pass | 17981/18000 | {'5xx': 19} | 9,743 | 11,301 | 12,559 | 224 | 1,780 | 2,382 | {'perf-mock--mock-a': 17981} |
| 10,500 | pass | 20984/21000 | {'5xx': 16} | 9,730 | 10,996 | 12,047 | 210 | 1,475 | 2,133 | {'perf-mock--mock-a': 20984} |
| 11,280 | pass | 22518/22560 | {'5xx': 42} | 9,789 | 11,804 | 12,980 | 270 | 2,283 | 2,628 | {'perf-mock--mock-a': 22518} |
| 12,000 | latency | 23961/24000 | {'5xx': 39} | 9,854 | 12,032 | 13,192 | 334 | 2,512 | 2,840 | {'perf-mock--mock-a': 23961} |

### aks-mock · rag: max 480 RPM (then {'rpm': 600, 'reason': 'errors'})  top step 600

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 300 | pass | 600/600 |  | 6,834 | 6,854 | 6,947 | 46 | 65 | 1,845 | {'perf-mock--mock-a': 600} |
| 480 | pass | 960/960 |  | 6,833 | 6,845 | 6,854 | 45 | 56 | 1,836 | {'perf-mock--mock-a': 960} |
| 600 | errors | 1168/1200 | {'connection': 32} | 6,833 | 6,847 | 6,864 | 46 | 58 | 1,838 | {'perf-mock--mock-a': 1168} |

### aks-mock · short: max ≥ 15000 RPM (then None)  top step 15000

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1200/1200 |  | 2,726 | 2,738 | 2,744 | 22 | 33 | - | {'perf-mock--mock-a': 1200} |
| 1,500 | pass | 2999/3000 | {'5xx': 1} | 2,724 | 2,735 | 2,742 | 21 | 29 | - | {'perf-mock--mock-a': 2999} |
| 3,000 | pass | 5999/6000 | {'5xx': 1} | 2,723 | 2,733 | 2,748 | 20 | 27 | - | {'perf-mock--mock-a': 5999} |
| 6,000 | pass | 12000/12000 |  | 2,722 | 2,729 | 2,736 | 19 | 24 | - | {'perf-mock--mock-a': 12000} |
| 9,000 | pass | 18000/18000 |  | 2,721 | 2,728 | 2,735 | 18 | 23 | - | {'perf-mock--mock-a': 18000} |
| 12,000 | pass | 23999/24000 | {'5xx': 1} | 2,721 | 2,728 | 2,733 | 17 | 22 | - | {'perf-mock--mock-a': 23999} |
| 15,000 | pass | 30000/30000 |  | 2,721 | 2,728 | 2,734 | 17 | 23 | - | {'perf-mock--mock-a': 30000} |

### failover aks-perf-mock at 1200 RPM

| phase | requests | error rate | errors | p50 | p95 | moved after | members |
|---|---|---|---|---|---|---|---|
| healthy | 1200 | 0.00% |  | 9,581 | 9,602 | - | {'perf-mock--mock-a': 1192, 'perf-mock--mock-b': 8} |
| mock-a 429 | 2400 | 0.00% |  | 9,581 | 9,606 | 0 s | {'perf-mock--mock-b': 2400} |
| mock-a 200 | 2400 | 0.04% | {'503': 1} | 9,581 | 9,600 | - | {'perf-mock--mock-a': 2384, 'perf-mock--mock-b': 15} |
| mock-a 500 | 2400 | 0.00% |  | 9,581 | 9,598 | 0 s | {'perf-mock--mock-b': 2400} |
| mock-a 200 | 2400 | 0.00% |  | 9,580 | 9,599 | - | {'perf-mock--mock-a': 2382, 'perf-mock--mock-b': 18} |
| mock-a hang | 2400 | 0.00% |  | 129,635 | 129,671 | 0 s | {'perf-mock--mock-b': 2398, 'perf-mock--mock-a': 2} |
| mock-a 200 | 2401 | 0.00% |  | 9,591 | 9,625 | - | {'perf-mock--mock-b': 2401} |

## aks-rag-1

### aks-mock-rerun · rag: max 14280 RPM (then {'rpm': 15000, 'reason': 'latency'})  top step 15000

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 600 | pass | 1200/1200 |  | 6,832 | 6,846 | 6,858 | 44 | 53 | 1,837 | {'perf-mock--mock-a': 1200} |
| 1,500 | pass | 3000/3000 |  | 6,833 | 6,845 | 6,860 | 45 | 52 | 1,833 | {'perf-mock--mock-a': 3000} |
| 3,000 | pass | 5999/6000 | {'5xx': 1} | 6,836 | 6,852 | 6,984 | 48 | 59 | 1,835 | {'perf-mock--mock-a': 5999} |
| 6,000 | pass | 11999/12000 | {'5xx': 1} | 6,848 | 6,884 | 7,682 | 60 | 92 | 1,852 | {'perf-mock--mock-a': 11999} |
| 9,000 | pass | 17996/18000 | {'5xx': 4} | 6,871 | 7,058 | 8,185 | 83 | 266 | 1,939 | {'perf-mock--mock-a': 17996} |
| 12,000 | pass | 23989/24000 | {'5xx': 11} | 6,909 | 7,798 | 8,912 | 121 | 1,006 | 2,367 | {'perf-mock--mock-a': 23989} |
| 13,500 | pass | 26984/27000 | {'5xx': 16} | 6,890 | 7,234 | 8,491 | 102 | 442 | 2,002 | {'perf-mock--mock-a': 26984} |
| 14,280 | pass | 28540/28560 | {'5xx': 20} | 6,893 | 7,398 | 8,638 | 105 | 605 | 2,099 | {'perf-mock--mock-a': 28540} |
| 15,000 | latency | 29972/30000 | {'5xx': 28} | 6,974 | 8,653 | 9,766 | 186 | 1,861 | 2,974 | {'perf-mock--mock-a': 29972} |

## aks-real-1

### aks-perf-luna · agent: max ≥ 8 RPM (then None)  top step 8

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 2 | pass | 4/4 |  | 4,449 | 4,951 | 4,951 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 4} |
| 4 | pass | 8/8 |  | 4,158 | 5,037 | 5,037 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 8} |
| 8 | pass | 16/16 |  | 4,776 | 6,296 | 6,296 | - | - | - | {'perf-luna--pool-control-plane-test-gpt-6-luna': 7, 'perf-luna--pool-openaiminggu-gpt-6-luna': 7, 'perf-luna--pool-ai-minggu3566ai776846365708-gpt-6-luna-dz': 2} |

### aks-perf-luna · chat: max ≥ 160 RPM (then None)  top step 160

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 20 | pass | 40/40 |  | 4,369 | 6,045 | 21,126 | - | - | 1,488 | {'perf-luna--pool-openaiminggu-gpt-6-luna': 35, 'perf-luna--pool-control-plane-test-gpt-6-luna': 5} |
| 40 | pass | 80/80 |  | 4,667 | 5,306 | 5,641 | - | - | 1,525 | {'perf-luna--pool-openaiminggu-gpt-6-luna': 80} |
| 80 | pass | 160/160 |  | 4,595 | 5,241 | 5,443 | - | - | 1,697 | {'perf-luna--pool-openaiminggu-gpt-6-luna': 160} |
| 160 | pass | 320/320 |  | 4,698 | 5,465 | 7,980 | - | - | 1,790 | {'perf-luna--pool-control-plane-test-gpt-6-luna': 161, 'perf-luna--pool-openaiminggu-gpt-6-luna': 159} |

### aks-perf-luna · short: max ≥ 360 RPM (then None)  top step 360

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 30 | pass | 60/60 |  | 2,402 | 3,495 | 9,807 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 38, 'perf-luna--pool-control-plane-test-gpt-6-luna': 22} |
| 60 | pass | 120/120 |  | 1,746 | 2,675 | 4,182 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 120} |
| 120 | pass | 240/240 |  | 1,643 | 2,383 | 3,336 | - | - | - | {'perf-luna--pool-openaiminggu-gpt-6-luna': 240} |
| 240 | pass | 480/480 |  | 1,666 | 2,420 | 4,304 | - | - | - | {'perf-luna--pool-control-plane-test-gpt-6-luna': 241, 'perf-luna--pool-openaiminggu-gpt-6-luna': 239} |
| 360 | pass | 720/720 |  | 1,766 | 7,298 | 12,531 | - | - | - | {'perf-luna--pool-control-plane-test-gpt-6-luna': 283, 'perf-luna--pool-openaiminggu-gpt-6-luna': 244, 'perf-luna--pool-ai-minggu3566ai776846365708-gpt-6-luna-dz': 139, 'perf-luna--pool-minggu-luna-eu-22610-gpt-6-luna-dz': 54} |

### direct-luna · agent: max 4 RPM (then {'rpm': 8, 'reason': 'errors'})  top step 8

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 2 | pass | 4/4 |  | 4,350 | 4,673 | 4,673 | - | - | - | {'Central US': 4} |
| 4 | pass | 8/8 |  | 4,192 | 5,660 | 5,660 | - | - | - | {'Central US': 8} |
| 8 | errors | 7/16 | {'429': 9} | 4,130 | 4,649 | 4,649 | - | - | - | {'Central US': 7} |

### direct-luna · chat: max 80 RPM (then {'rpm': 160, 'reason': 'errors'})  top step 160

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 20 | pass | 40/40 |  | 4,075 | 4,590 | 6,274 | - | - | 1,174 | {'Central US': 40} |
| 40 | pass | 80/80 |  | 4,168 | 4,890 | 6,948 | - | - | 1,004 | {'Central US': 80} |
| 80 | pass | 160/160 |  | 4,042 | 4,651 | 4,825 | - | - | 1,011 | {'Central US': 160} |
| 160 | errors | 240/320 | {'429': 80} | 4,190 | 4,868 | 5,050 | - | - | 1,012 | {'Central US': 240} |

### direct-luna · short: max 120 RPM (then {'rpm': 240, 'reason': 'errors'})  top step 360

| RPM | verdict | ok | errors | p50 | p95 | p99 | +p50 | +p95 | TTFT p95 | members |
|---|---|---|---|---|---|---|---|---|---|---|
| 30 | pass | 60/60 |  | 1,611 | 2,287 | 2,479 | - | - | - | {'Central US': 60} |
| 60 | pass | 120/120 |  | 1,581 | 2,186 | 2,801 | - | - | - | {'Central US': 120} |
| 120 | pass | 240/240 |  | 1,514 | 2,176 | 4,171 | - | - | - | {'Central US': 240} |
| 240 | errors | 335/480 | {'429': 145} | 1,542 | 2,202 | 2,811 | - | - | - | {'Central US': 335} |
| 360 | errors | 248/720 | {'429': 472} | 1,545 | 2,291 | 4,345 | - | - | - | {'Central US': 248} |

