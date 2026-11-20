"""Control target: sleeps in 1 s slices for 60 s (compare with one sleep(60))."""
import time

for _ in range(60):
    time.sleep(1)
