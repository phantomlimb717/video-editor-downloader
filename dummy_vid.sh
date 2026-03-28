#!/bin/bash
ffmpeg -f lavfi -i testsrc=duration=1:size=320x240:rate=10 -c:v libx264 dummy.mp4 -y
