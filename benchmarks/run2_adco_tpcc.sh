#!/bin/bash

CMDRunTPCC="./benchmarks/scripts/tpcc.sh"
CMDRunADCo="./benchmarks/scripts/adco.sh"
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
$CMDRunADCo tpcc postgres tpcc
$CMDDocker Restart ${db_container}
sleep 3s
echo "----------------->> Optimized <<-----------------"
$CMDRunTPCC tpcc_adco adco.csv