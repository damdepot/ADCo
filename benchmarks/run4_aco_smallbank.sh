#!/bin/bash

CMDRunSmallbank="./benchmarks/scripts/smallbank.sh"

echo "----------------->> baseline <<-----------------"
$CMDRunSmallbank baseline
echo "----------------->> optimized <<-----------------"
$CMDRunSmallbank smallbank