#!/bin/bash

CMDRunTPCC="./benchmarks/scripts/tpcc.sh"
CMDRunDCo="./benchmarks/scripts/dco.sh"
CMDDocker="./benchmarks/scripts/docker.sh"

db_container="adcoexp-db"
db_service="pgdb"


echo "----------------->> Refreshing Docker <<-----------------"
$CMDDocker Down ${db_container}
$CMDDocker Up ${db_container} ${db_service}
sleep 5s
echo "----------------->> Baseline <<-----------------"
$CMDRunTPCC baseline baseline.csv
echo "----------------->> DCo <<-----------------"
$CMDRunDCo tpcc postgres tpcc
$CMDDocker Restart ${db_container}
sleep 3s
echo "----------------->> Optimized <<-----------------"
$CMDRunTPCC baseline dco.csv